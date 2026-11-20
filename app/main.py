"""StrayWatch web server: website, browser chat, public lost posters, WhatsApp webhook, escalation timer."""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import config, engine, push
from .channels import whatsapp
from .db import (Animal, Case, CaseEvent, CheckIn, Consult, LostPost, Outbox, PartnerLead, Photo, SessionLocal, Sighting,
                 Trace, User, VetProfile, init_db)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("straywatch")
HERE = Path(__file__).resolve().parent
@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as s:
        n = engine.seed_translations(s)
        if n:
            log.info("loaded %s pre-made translations", n)
    if config.DEMO_MODE:
        from .seed import seed
        await asyncio.to_thread(seed)
    task = asyncio.create_task(escalation_loop())
    yield
    task.cancel()


app = FastAPI(title="StrayWatch", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1000)  # pages and scripts load faster on slow phones
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
_lock = threading.Lock()  # one conversation step at a time keeps SQLite happy and state consistent
_seen_wa_ids: OrderedDict[str, None] = OrderedDict()


# ------------------------------------------------------------------ delivery

def deliver(s, outs: list[engine.Out], viewer: str | None = None) -> list[dict]:
    """Translate, then send each message to its recipient: WhatsApp users via the Cloud API, everyone else to an
    outbox the browser polls. Messages for `viewer` are returned directly."""
    outs = engine.localise(s, outs)
    mine, pinged = [], set()
    for o in outs:
        if o.to == viewer:
            mine.append(o.as_dict())
        elif o.to.startswith("wa:") and whatsapp.enabled():
            if not whatsapp.send(o.to[3:], o):
                _undelivered(s, o)
        else:
            s.add(Outbox(user_id=o.to, payload=o.as_dict()))
            # Phone notification, once per person per update, so they see it even with the app closed.
            if o.to not in pinged and not o.to.startswith(("demo:", "camera:")) and push.has_subscription(s, o.to):
                pinged.add(o.to)
                link = o.link if (o.link or "").startswith(config.PUBLIC_BASE_URL + "/track/") else "/app"
                push.notify(o.to, o.text, link.replace(config.PUBLIC_BASE_URL, "") or "/app")
    return mine


def _undelivered(s, o: engine.Out):
    """A rescue alert that WhatsApp refused (e.g. helper outside the 24-hour window): try the approved template,
    and don't let the rescue wait 3 minutes for someone who never got the message."""
    cid = next((b[0].split(":")[2] for b in o.buttons if b[0].startswith("case:go:")), None)
    if not cid:
        return
    if whatsapp.send_alert_template(o.to[3:], o.text.split("\n")[1] if "\n" in o.text else o.text):
        return
    c = s.get(Case, int(cid))
    if c and c.status == "alerting" and c.current_helper == o.to:
        c.alerted_at = c.alerted_at.replace(year=2000)  # escalation loop moves on at the next tick (<= 20 s)
        s.add(Trace(user_id=c.reporter_id, step="Alert not delivered", detail=f"SW-{c.id}: skipping to the next helper"))


def run_turn(user_id: str, inc: engine.Incoming, viewer: str | None) -> list[dict]:
    with _lock, SessionLocal() as s:
        try:
            outs = engine.handle(s, user_id, inc)
            mine = deliver(s, outs, viewer)
            s.commit()
            return mine
        except Exception:
            s.rollback()
            log.exception("turn failed")
            return [{"to": user_id, "text": "Sorry, something went wrong on my side. Please try again. 🙏",
                     "buttons": [{"id": "menu", "title": "Menu"}], "options": [], "image_url": None, "link": None,
                     "ask_location": False}]


async def escalation_loop():
    while True:
        await asyncio.sleep(20)
        try:
            await asyncio.to_thread(_escalate_once)
        except Exception:  # noqa: BLE001
            log.exception("escalation tick failed")


def _escalate_once():
    with _lock, SessionLocal() as s:
        outs = engine.escalate_due(s)
        if outs:
            deliver(s, outs)
        s.commit()


# ------------------------------------------------------------------ pages

def stats() -> dict:
    with SessionLocal() as s:
        cases = list(s.scalars(select(Case)))
        acc = sorted((c.accepted_at - c.created).total_seconds() / 60 for c in cases if c.accepted_at)
        found = list(s.scalars(select(LostPost).where(LostPost.status == "found")))
        return {
            "animals": s.scalar(select(func.count()).select_from(Animal)) or 0,
            "checkins": s.scalar(select(func.count()).select_from(CheckIn)) or 0,
            "cases": len(cases),
            "helped": sum(1 for c in cases if c.status in ("assigned", "closed")),
            "median_accept_min": round(acc[len(acc) // 2], 1) if acc else None,
            "found": len(found),
            "helpers": s.scalar(select(func.count()).select_from(User).where(User.is_volunteer.is_(True))) or 0,
            "closed": sum(1 for c in cases if c.status == "closed"),
            "camera": sum(1 for c in cases if c.source == "camera"),
            "responders": s.scalar(select(func.count()).select_from(User).where(
                (User.is_volunteer.is_(True)) | (User.role.is_not(None)))) or 0,
        }


def public_posts(limit=12) -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(LostPost).where(LostPost.public.is_(True)).order_by(LostPost.status.desc(), LostPost.created.desc()).limit(limit))
        return [post_card(p) for p in rows]


def post_card(p: LostPost) -> dict:
    return {"code": p.code, "name": p.name, "species": p.species, "status": p.status,
            "looks": engine.describe_line(p.profile), "photo": f"/photo/{p.photo_id}" if p.photo_id else None,
            "area": p.area_label or f"near {round(p.lat, config.PUBLIC_COORD_DECIMALS)}, {round(p.lon, config.PUBLIC_COORD_DECIMALS)}",
            "when": p.last_seen_text or p.created.strftime("%d %b"), "created": p.created.strftime("%d %b"),
            "found_hours": round((p.found_at - p.created).total_seconds() / 3600) if p.found_at else None}


def page(request: Request, name: str, active: str, **ctx):
    return templates.TemplateResponse(request, name, {"active": active, "demo": config.DEMO_MODE,
                                                      "wa_number": config.WA_DISPLAY_NUMBER, **ctx})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, "home.html", "/", stats=stats())


@app.get("/demo", response_class=HTMLResponse)
def demo_page(request: Request):
    return page(request, "demo.html", "/demo")


@app.get("/how", response_class=HTMLResponse)
def how_page(request: Request):
    return page(request, "how.html", "/how")


@app.get("/room", response_class=HTMLResponse)
def room_page(request: Request):
    return page(request, "room.html", "/room")


@app.get("/camera", response_class=HTMLResponse)
def camera_page(request: Request):
    with SessionLocal() as s:
        cams = [case_view(s, c) for c in s.scalars(select(Case).where(Case.source == "camera").order_by(Case.id.desc()).limit(6))]
        boards = engine.signboards(s)
    return page(request, "camera.html", "/camera", cases=cams, boards=boards)


@app.get("/lost", response_class=HTMLResponse)
def lost_page(request: Request):
    return page(request, "lost.html", "/lost", posts=public_posts(24))


@app.get("/vet", response_class=HTMLResponse)
def vet_page(request: Request, joined: str = ""):
    with SessionLocal() as s:
        online = list(s.scalars(select(VetProfile).where(VetProfile.verified.is_(True), VetProfile.online.is_(True),
                                                         VetProfile.user_id.is_not(None)).order_by(VetProfile.fee)))
        j = s.scalars(select(VetProfile).where(VetProfile.code == joined.upper())).first() if joined else None
        return page(request, "vet.html", "/vet", online=online, joined=j)


@app.post("/vet/join")
def vet_join(name: str = Form(...), council: str = Form(...), reg_no: str = Form(...), contact: str = Form(...),
             clinic: str = Form(""), qualification: str = Form(""), langs: str = Form(""), fee: int = Form(0),
             free_for_strays: str = Form(""), consent: str = Form("")):
    """A vet signs up. They are listed only after a person checks the registration (see /admin/vets)."""
    from fastapi.responses import RedirectResponse
    from . import vet as vetmod
    if consent != "yes" or not name.strip() or not reg_no.strip():
        raise HTTPException(400, "Please fill in your name and registration, and tick the consent box.")
    known = {"en", "hi", "bn", "ta", "te", "mr"}
    ls = [x.strip().lower()[:2] for x in langs.replace(";", ",").split(",") if x.strip()[:2].lower() in known] or ["en", "hi"]
    with _lock, SessionLocal() as s:
        code = vetmod.new_code(s)
        s.add(VetProfile(code=code, name=name.strip()[:80], clinic=clinic.strip()[:120] or None,
                         qualification=qualification.strip()[:80] or None, council=council.strip()[:80],
                         reg_no=reg_no.strip()[:40], contact=contact.strip()[:120], langs=ls, species=["any"],
                         fee=max(0, min(2000, fee)), free_for_strays=free_for_strays == "yes", verified=False))
        s.commit()
    return RedirectResponse(f"/vet?joined={code}#join", status_code=303)


@app.get("/admin/vets", response_class=HTMLResponse)
def admin_vets(request: Request, key: str = ""):
    """Verify vets' registrations. Needs SW_ADMIN_KEY."""
    if not config.ADMIN_KEY or not secrets.compare_digest(key, config.ADMIN_KEY):
        raise HTTPException(404)
    with SessionLocal() as s:
        vets = list(s.scalars(select(VetProfile).where(VetProfile.sample.is_(False)).order_by(VetProfile.verified, VetProfile.id)))
        return page(request, "admin_vets.html", None, vets=vets, key=key)


@app.post("/admin/vets/{vid}/verify")
def admin_verify(vid: int, key: str = Form(""), on: str = Form("yes")):
    from fastapi.responses import RedirectResponse
    if not config.ADMIN_KEY or not secrets.compare_digest(key, config.ADMIN_KEY):
        raise HTTPException(404)
    with _lock, SessionLocal() as s:
        v = s.get(VetProfile, vid)
        if not v:
            raise HTTPException(404)
        v.verified = on == "yes"
        if not v.verified:
            v.online = False
        outs = []
        if v.verified and v.user_id:
            outs.append(engine.Out(v.user_id, f"✅ {v.name}, your registration is verified! Type *online* to start "
                                              "receiving consults, *offline* to stop."))
        deliver(s, outs)
        s.commit()
    return RedirectResponse(f"/admin/vets?key={key}", status_code=303)


@app.get("/promise", response_class=HTMLResponse)
def promise_page(request: Request):
    return page(request, "promise.html", "/promise")


def _test_count() -> int:
    tests = HERE.parent / "tests"
    return sum(f.read_text(encoding="utf-8").count("\ndef test_") for f in tests.glob("test_*.py")) if tests.exists() else 0


@app.get("/video", response_class=HTMLResponse)
def video_page(request: Request):
    return page(request, "video.html", "/video")


@app.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request):
    return page(request, "docs.html", "/docs", tests=_test_count())


@app.get("/partners", response_class=HTMLResponse)
def partners_page(request: Request, sent: int = 0):
    return page(request, "partners.html", "/partners", sent=bool(sent))


@app.post("/partners")
def partners_submit(name: str = Form(...), kind: str = Form(...), contact: str = Form(...), org: str = Form(""),
                    area: str = Form(""), note: str = Form(""), consent: str = Form("")):
    from fastapi.responses import RedirectResponse
    if consent != "yes" or not name.strip() or not contact.strip():
        raise HTTPException(400, "Please fill in your name and contact, and tick the consent box.")
    kinds = {"ngo", "vet", "municipal", "police", "community", "volunteer", "other"}
    with _lock, SessionLocal() as s:
        s.add(PartnerLead(name=name.strip()[:80], org=org.strip()[:120] or None, kind=kind if kind in kinds else "other",
                          contact=contact.strip()[:120], area=area.strip()[:80] or None, note=note.strip()[:800] or None))
        s.commit()
    return RedirectResponse("/partners?sent=1#main", status_code=303)


def city_view() -> dict:
    """Aggregates for NGOs and municipalities. Areas are ~1 km grid cells; no individual locations are shown."""
    from datetime import date, timedelta as td
    from .db import now as _now
    with SessionLocal() as s:
        animals = [a for a in s.scalars(select(Animal)) if a.status == "active"]
        dogs = [a for a in animals if a.species == "dog"]

        def vaccinated(a):
            v = a.rabies_vaccinated_on or ""
            return v[:4].isdigit() and (date.today() - date.fromisoformat(v)).days <= 365
        vax = sum(1 for a in animals if a.species in ("dog", "cat") and vaccinated(a))
        vax_base = sum(1 for a in animals if a.species in ("dog", "cat"))
        notched = sum(1 for a in dogs if (a.profile or {}).get("ear_notch"))
        cases = list(s.scalars(select(Case)))
        acc = sorted((c.accepted_at - c.created).total_seconds() / 60 for c in cases if c.accepted_at)
        posts = list(s.scalars(select(LostPost)))
        found = sorted((p.found_at - p.created).total_seconds() / 3600 for p in posts if p.found_at)
        since = _now() - td(days=30)
        cells: dict[tuple, dict] = {}

        def cell(lat, lon):
            k = (round(lat, 2), round(lon, 2))
            return cells.setdefault(k, {"area": f"{k[0]:.2f}, {k[1]:.2f}", "sightings": 0, "cattle_road": 0, "sos": 0,
                                        "unvaccinated": 0, "map": f"https://maps.google.com/?q={k[0]},{k[1]}"})
        for x in s.scalars(select(Sighting).where(Sighting.created >= since)):
            c = cell(x.lat, x.lon)
            c["sightings"] += 1
            if x.species in ("cow", "buffalo") and (x.profile or {}).get("on_road"):
                c["cattle_road"] += 1
        for c in cases:
            if c.created >= since:
                cell(c.lat, c.lon)["sos"] += 1
        for a in animals:
            if a.lat and a.species in ("dog", "cat") and not vaccinated(a):
                cell(a.lat, a.lon)["unvaccinated"] += 1
        actions = []
        for c in cells.values():
            if c["cattle_road"] >= 2:
                actions.append(("Cattle gather on the road", c, f"{c['cattle_road']} sightings of cattle on the road (traffic risk): reflective collars, a feeding spot away from the road, owner outreach"))
            if c["unvaccinated"] >= 2:
                actions.append(("Vaccination drive", c, f"{c['unvaccinated']} known animals not vaccinated in the last year"))
            if c["sos"] >= 2:
                actions.append(("Rescue hotspot", c, f"{c['sos']} SOS cases in 30 days: ask for more helpers here"))
        hot = sorted(cells.values(), key=lambda c: (c["sos"] * 3 + c["cattle_road"] * 2 + c["sightings"]), reverse=True)[:6]
        return {
            "animals": len(animals), "dogs": len(dogs), "cows": sum(1 for a in animals if a.species == "cow"),
            "cats": sum(1 for a in animals if a.species == "cat"),
            "vax_pct": round(100 * vax / vax_base) if vax_base else 0, "vax": vax, "vax_base": vax_base,
            "notch_pct": round(100 * notched / len(dogs)) if dogs else 0, "notched": notched,
            "checkins_7d": s.scalar(select(func.count()).select_from(CheckIn).where(CheckIn.created >= _now() - td(days=7))) or 0,
            "cases": len(cases), "closed": sum(1 for c in cases if c.status == "closed"),
            "high": sum(1 for c in cases if c.level == "HIGH"), "check": sum(1 for c in cases if c.level == "CHECK"),
            "low": sum(1 for c in cases if c.level == "LOW"),
            "dispatched": sum(1 for c in cases if c.status != "logged"),
            "proofed": sum(1 for c in cases if c.status == "closed" and c.proof_photo_id),
            "proof_pct": round(100 * sum(1 for c in cases if c.status == "closed" and c.proof_photo_id) /
                               max(1, sum(1 for c in cases if c.status == "closed"))),
            "camera_cases": sum(1 for c in cases if c.source == "camera"),
            "assigned": sum(1 for c in cases if c.status in ("assigned", "closed")),
            "unanswered": sum(1 for c in cases if c.status == "unanswered"),
            "median_accept": round(acc[len(acc) // 2], 1) if acc else None,
            "lost": len(posts), "found": len(found), "median_found_h": round(found[len(found) // 2]) if found else None,
            "helpers": s.scalar(select(func.count()).select_from(User).where(User.is_volunteer.is_(True))) or 0,
            "hot": hot, "actions": actions,
        }


@app.get("/city", response_class=HTMLResponse)
def city(request: Request):
    with SessionLocal() as s:
        hot = engine.hotspot_actions(s)
    return page(request, "city.html", "/city", c=city_view(), hotspots=hot)


@app.get("/app", response_class=HTMLResponse)
def app_screen(request: Request, embed: int = 0):
    """The installable app: chat, lost & found, more. Same engine as the website demo and WhatsApp.
    embed=1: shown inside the floating chat panel on every website page."""
    return templates.TemplateResponse(request, "app.html", {"posts": public_posts(), "wa_number": config.WA_DISPLAY_NUMBER,
                                                            "embed": bool(embed)})


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "StrayWatch", "short_name": "StrayWatch",
        "description": "Look after community animals, get help fast when one is hurt, and find lost animals.",
        "start_url": "/app", "scope": "/", "display": "standalone",
        "background_color": "#FFF8EC", "theme_color": "#FFD23F",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }, media_type="application/manifest+json")


SW = """const C='sw-v2';const SHELL=['/app','/static/style.css','/static/app.js','/static/favicon.svg'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)));self.skipWaiting();});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(e.request.method!=='GET'||u.pathname.startsWith('/api/'))return;
 e.respondWith(fetch(e.request).then(r=>{if(SHELL.includes(u.pathname)){const k=r.clone();caches.open(C).then(c=>c.put(e.request,k));}return r;})
 .catch(()=>caches.match(e.request)));});
self.addEventListener('push',e=>{let d={};try{d=e.data.json();}catch(x){d={title:'StrayWatch',body:e.data?e.data.text():''};}
 e.waitUntil(self.registration.showNotification(d.title||'StrayWatch',{body:d.body||'',icon:'/static/icon-192.png',
 badge:'/static/icon-192.png',data:{url:d.url||'/app'},vibrate:[200,100,200],tag:'sw-'+(d.url||'app'),renotify:true}));});
self.addEventListener('notificationclick',e=>{e.notification.close();const u=(e.notification.data&&e.notification.data.url)||'/app';
 e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ws=>{for(const w of ws){if(w.url.includes(u)&&'focus' in w)return w.focus();}
 return clients.openWindow(u);}));});"""


@app.get("/sw.js")
def service_worker():
    """Lets the app install on a phone's home screen and open its shell offline. Chat itself needs the network."""
    return Response(SW, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/lost/{code}", response_class=HTMLResponse)
def poster(request: Request, code: str):
    with SessionLocal() as s:
        p = s.scalar(select(LostPost).where(LostPost.code == code.upper()))
        if not p or not p.public:
            raise HTTPException(404, "Poster not found")
        card = post_card(p)
    seen_link = (f"https://wa.me/{config.WA_DISPLAY_NUMBER}?text=SEEN%20{card['code']}" if config.WA_DISPLAY_NUMBER
                 else f"/app?seen={card['code']}")
    # Bilingual poster: English plus the owner's language (Hindi if the owner uses English), for local groups.
    with SessionLocal() as s:
        post = s.scalar(select(LostPost).where(LostPost.code == code.upper()))
        owner = s.get(User, post.owner_id) if post else None
        lang = (owner.lang if owner and owner.lang and owner.lang != "en" else "hi")
        en = {"q": f"Have you seen {card['name']}?", "home": f"{card['name']} is home!", "thanks": "thank you, neighbours 💛",
              "animal": "Animal", "looks": "Looks", "last": "Last seen", "when": "When",
              "seen": f"I've seen {card['name']}!", "share": "Share this poster",
              "how": "The button above opens a chat with StrayWatch. The owner's number is never shown.",
              "species": card["species"].title(), "looks_v": card["looks"] or "See photo"}
        tr = engine.translate_texts(s, list(en.values()), lang)
        s.commit()
    L = {k: tr.get(v, v) for k, v in en.items()}
    return templates.TemplateResponse(request, "poster.html", {"p": card, "seen_link": seen_link, "E": en, "L": L,
                                                               "lang": lang, "base": config.PUBLIC_BASE_URL})


AGENT_LABELS = {"Triage": "Triage agent", "Rules": "Urgency rules (code)", "Dispatch": "Dispatch agent",
                "Watch": "Watch agent", "Identify": "Identify agent", "Camera": "Camera agent",
                "Hotspot": "Hotspot agent", "Person": "Person"}
STATUS_WORDS = {"alerting": "Finding help", "assigned": "Help on the way", "closed": "Safe · closed",
                "unanswered": "No responder free yet", "logged": "Healthy · no case", "cancelled": "Cancelled"}


def ist_str(dt) -> str:
    return engine.ist(dt)


def case_view(s, c: Case) -> dict:
    """What a case looks like to people: plain words, no phone numbers, area rounded to ~1 km."""
    from . import rules
    t = c.triage or {}
    helper = s.get(User, c.assigned_to) if c.assigned_to else None
    who = None
    if helper:
        team, is_ct = engine.responder_label(s, c, helper.id)
        who = "The animal's caretaker" if is_ct else (helper.org or rules.TEAM_LABELS.get(team, "A helper").capitalize())
    head, nxt = {
        "alerting": ("Finding help now", f"Responder #{len(c.asked or [])} has been alerted. If they can't reply in "
                                          f"{engine.hop_words(c)}, StrayWatch asks the next one by itself."),
        "assigned": ("Help is on the way 🚶", f"{who or 'A responder'} accepted. Please stay at a safe distance until they arrive."),
        "closed": ("The animal is safe 🎉", "Thank you for reporting. You made this happen."),
        "unanswered": ("No responder is free yet", "Please call the government animal ambulance on 1962. StrayWatch keeps "
                                                   "the case open and alerts new responders nearby."),
        "cancelled": ("Case cancelled", "You told us the animal had gone, so the responders stood down. Thank you."),
        "logged": ("The animal looked healthy", "No rescue was needed, so nobody was disturbed. It's counted on the "
                                              "hotspot map. If it gets worse, tell StrayWatch in the chat."),
    }.get(c.status, ("Case", ""))
    return {"code": f"SW-{c.id}", "id": c.id, "level": c.level or "HIGH",
            "level_words": engine.LEVEL_WORDS.get(c.level or "HIGH", "URGENT"), "status": c.status,
            "status_words": STATUS_WORDS.get(c.status, c.status), "species": (t.get("species") or "animal").title(),
            "summary": t.get("summary", ""), "created": ist_str(c.created), "headline": head, "next": nxt,
            "area": f"near {round(c.lat, config.PUBLIC_COORD_DECIMALS)}, {round(c.lon, config.PUBLIC_COORD_DECIMALS)}",
            "photo": f"/photo/{c.photo_id}" if c.photo_id else None,
            "proof": f"/photo/{c.proof_photo_id}" if c.proof_photo_id else None, "proof_note": c.proof_note,
            "reasons": c.reasons or [], "teams": [rules.TEAM_LABELS.get(x, x) for x in (c.teams or [])],
            "source": c.source or "citizen", "reports": c.merged_reports or 1, "who": who,
            "minutes_to_accept": round((c.accepted_at - c.created).total_seconds() / 60, 1) if c.accepted_at else None}


def case_events(s, c: Case, public_only=True) -> list[dict]:
    q = select(CaseEvent).where(CaseEvent.case_id == c.id).order_by(CaseEvent.id)
    from datetime import timedelta as td
    return [{"at": (e.created + td(hours=5, minutes=30)).strftime("%d %b, %H:%M:%S") if not public_only else ist_str(e.created), "agent": e.agent,
             "agent_label": AGENT_LABELS.get(e.agent, e.agent), "kind": e.kind, "detail": e.detail}
            for e in s.scalars(q) if e.public or not public_only]


@app.get("/api/room")
def api_room():
    """Agent Room board: recent cases (no names or numbers, areas rounded) and agent activity in the last 24 h."""
    from datetime import timedelta as td
    from .db import now as _now
    with SessionLocal() as s:
        cases = list(s.scalars(select(Case).order_by(Case.id.desc()).limit(40)))
        since = _now() - td(days=1)
        counts = dict(s.execute(select(CaseEvent.agent, func.count()).where(CaseEvent.created >= since)
                                .group_by(CaseEvent.agent)).all())
        return {"cases": [case_view(s, c) for c in cases], "agents": counts}


@app.get("/api/room/case/{cid}")
def api_room_case(cid: int):
    with SessionLocal() as s:
        c = s.get(Case, cid)
        if not c:
            raise HTTPException(404)
        return {"case": case_view(s, c), "events": case_events(s, c, public_only=False)}


TRACK_STRINGS = ["your report", "Case", "reported", "Every step, as it happened",
                 "This page refreshes by itself every 20 seconds.", "Your photo", "Proof photo from the responder",
                 "Stay safe: don't step into traffic or touch a scared animal.", "Animal ambulance (govt.)",
                 "If a person is hurt"]


@app.get("/track/{code}", response_class=HTMLResponse)
def track(request: Request, code: str, k: str = ""):
    """The reporter's private tracking page (link sent in the chat)."""
    try:
        cid = int(code.upper().removeprefix("SW-"))
    except ValueError:
        raise HTTPException(404, "Case not found")
    with SessionLocal() as s:
        c = s.get(Case, cid)
        if not c or not c.track_key or not secrets.compare_digest(k, c.track_key):
            raise HTTPException(404, "Case not found")
        view, events = case_view(s, c), case_events(s, c)
        rep = s.get(User, c.reporter_id)
        lang = (rep.lang if rep else None) or "en"
        T = {k: k for k in TRACK_STRINGS}
        if lang != "en":
            texts = list(TRACK_STRINGS) + [view[k] for k in ("headline", "next", "level_words", "status_words", "species")] \
                + [e["detail"] for e in events] + [e["agent_label"] for e in events]
            tr = engine.translate_texts(s, texts, lang)
            s.commit()
            T = {k: tr.get(k, k) for k in TRACK_STRINGS}
            for k in ("headline", "next", "level_words", "status_words", "species"):
                view[k] = tr.get(view[k], view[k])
            for e in events:
                e["detail"], e["agent_label"] = tr.get(e["detail"], e["detail"]), tr.get(e["agent_label"], e["agent_label"])
        return templates.TemplateResponse(request, "track.html", {"c": view, "events": events, "active": None,
                                                                  "T": T, "lang": lang})


@app.get("/photo/{pid}")
def photo(pid: str):
    with SessionLocal() as s:
        p = s.get(Photo, pid)
        if not p:
            raise HTTPException(404)
        return Response(p.data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------------ browser chat

def _web_user(request: Request, response: Response) -> str:
    tok = request.cookies.get("sw")
    if not tok or len(tok) > 40:
        tok = secrets.token_urlsafe(16)
        response.set_cookie("sw", tok, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return "web:" + tok


@app.post("/api/chat")
async def chat(request: Request, response: Response, text: str = Form(""), button: str = Form(""),
               lat: float | None = Form(None), lon: float | None = Form(None), as_user: str = Form(""),
               demo: str = Form(""), photo: UploadFile | None = File(None)):
    me = _web_user(request, response)
    uid = me
    if as_user:  # the demo caretaker/helper phones on the website
        if not as_user.startswith("demo:"):
            raise HTTPException(403)
        uid = as_user
    img = await photo.read() if photo is not None else None
    if img and len(img) > 12 * 1024 * 1024:
        raise HTTPException(413, "Photo too large")
    inc = engine.Incoming(text=text[:1000], image=img or None, lat=lat, lon=lon, button=button[:120] or None)
    # The demo page (and its sample responders) runs inside a sealed demo; everything else is real use.
    config.DEMO_RUN.set(config.DEMO_MODE and (demo == "1" or uid.startswith("demo:")))
    msgs = await asyncio.to_thread(run_turn, uid, inc, uid)
    return {"messages": msgs, "me": uid}


# ------------------------------------------------------------------ agent protocol (edge agents → Agent Room)

def _camera_event(ev: dict) -> dict:
    import base64
    cam_id = str(ev.get("camera_id", ""))
    if cam_id not in engine.CAMERAS:
        raise HTTPException(400, "unknown camera")
    try:
        img = base64.b64decode(ev.get("snapshot_jpeg_b64", ""), validate=True)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "bad snapshot")
    if not img or len(img) > 4 * 1024 * 1024:
        raise HTTPException(400, "snapshot missing or too large")
    with _lock, SessionLocal() as s:
        try:
            case, outs = engine.camera_case(s, cam_id, ev, img)
            deliver(s, outs)
            s.commit()
        except Exception:
            s.rollback()
            log.exception("camera event failed")
            raise HTTPException(500, "could not open a case")
        return {"case": f"SW-{case.id}" if case else None, "level": case.level if case else None,
                "status": case.status if case else None,
                "summary": f"SW-{case.id} · {case.level} · {case.status}" if case else
                "no case: a healthy animal, logged for the traffic map, nobody dispatched"}


@app.post("/api/agents/camera/events")
async def camera_event(request: Request):
    """Edge Camera agents post here (deck: agent protocol over REST). Needs X-Agent-Key unless in demo mode."""
    key = request.headers.get("X-Agent-Key", "")
    if config.AGENT_KEY:
        if not secrets.compare_digest(key, config.AGENT_KEY):
            raise HTTPException(401, "bad agent key")
    elif not config.DEMO_MODE:
        raise HTTPException(401, "set SW_AGENT_KEY to accept camera events")
    ev = await request.json()
    return await asyncio.to_thread(_camera_event, ev)


@app.post("/api/agents/camera/replay")
async def camera_replay(which: str = "lying"):
    """Demo only: replay an event the edge agent really produced (data/camera_samples).
    'lying' = dogs lying on a road (opens a case); 'standing' = healthy cows walking (ignored)."""
    if not config.DEMO_MODE:
        raise HTTPException(404)
    sample = HERE.parent / "data" / "camera_samples" / ("event_standing.json" if which == "standing" else "event_lying.json")
    if not sample.exists():
        raise HTTPException(404, "no recorded sample")
    import json as _json
    config.DEMO_RUN.set(True)
    return await asyncio.to_thread(_camera_event, _json.loads(sample.read_text()))


@app.get("/api/map")
def api_map(days: int = 30):
    """Map layers for the city dashboard and Agent Room. Every location is rounded to ~1 km; no people are shown."""
    from datetime import timedelta as td
    from .db import now as _now
    since = _now() - td(days=max(1, min(days, 90)))
    rnd = lambda x: round(x, config.PUBLIC_COORD_DECIMALS)
    with SessionLocal() as s:
        cases = []
        for c in s.scalars(select(Case).where(Case.created >= since).order_by(Case.id.desc()).limit(400)):
            t = c.triage or {}
            cases.append({"id": c.id, "code": f"SW-{c.id}", "lat": rnd(c.lat), "lon": rnd(c.lon), "level": c.level or "HIGH",
                          "status": c.status, "species": t.get("species", "animal"), "source": c.source or "citizen",
                          "on_road": bool(t.get("on_road")), "when": engine.ist(c.created)})
        cattle = {}
        for x in s.scalars(select(Sighting).where(Sighting.created >= since)):
            if x.species in ("cow", "buffalo") and (x.profile or {}).get("on_road"):
                k = (rnd(x.lat), rnd(x.lon))
                cattle[k] = cattle.get(k, 0) + 1
        hot = engine.hotspot_actions(s, days)
        hot_pts = []
        for h in hot:
            p = next((pl for pl in engine.PLACES if pl[0] == h["place"]), None)
            if p:
                hot_pts.append({"lat": p[1], "lon": p[2], "count": h["count"], "action": h["action"], "why": h["why"]})
        cams = [{"id": k, "name": v["name"], "lat": v["lat"], "lon": v["lon"]} for k, v in engine.CAMERAS.items()]
        return {"cases": cases, "hotspots": hot_pts, "cameras": cams, "center": list(config.CITY_CENTER),
                "cattle": [{"lat": k[0], "lon": k[1], "count": n} for k, n in cattle.items()]}


@app.get("/api/signboards")
def api_signboards():
    """JSON feed for roadside signboards near hotspot cameras."""
    with SessionLocal() as s:
        return {"boards": engine.signboards(s)}


@app.get("/api/push/key")
def push_key():
    return {"key": push.keys()}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, response: Response):
    uid = _web_user(request, response)
    sub = await request.json()
    with _lock, SessionLocal() as s:
        ok = push.subscribe(s, uid, sub if isinstance(sub, dict) else {})
        s.commit()
    if not ok:
        raise HTTPException(400, "bad subscription")
    return {"ok": True}


@app.get("/api/my/cases")
def my_cases(request: Request, response: Response):
    """The reporter's own cases, newest first, with their private tracking links."""
    uid = _web_user(request, response)
    with SessionLocal() as s:
        rows = list(s.scalars(select(Case).where(Case.reporter_id == uid).order_by(Case.id.desc()).limit(20)))
        return {"cases": [{**{k: v for k, v in case_view(s, c).items() if k in ("code", "level", "level_words", "status",
                                                                               "status_words", "species", "created", "photo")},
                           "track": f"/track/SW-{c.id}?k={c.track_key}"} for c in rows]}


@app.get("/api/inbox")
def inbox(request: Request, response: Response, as_user: str = ""):
    uid = as_user if as_user.startswith("demo:") else _web_user(request, response)
    with _lock, SessionLocal() as s:
        rows = list(s.scalars(select(Outbox).where(Outbox.user_id == uid).order_by(Outbox.id)))
        for r in rows:
            s.delete(r)
        s.commit()
        return {"messages": [r.payload for r in rows]}


@app.get("/api/trace")
def trace(request: Request, response: Response, after: int = 0):
    uid = _web_user(request, response)
    with SessionLocal() as s:
        rows = s.scalars(select(Trace).where(Trace.user_id == uid, Trace.id > after).order_by(Trace.id).limit(60))
        return {"steps": [{"id": t.id, "step": t.step, "detail": t.detail[:400], "model": t.model, "ms": t.ms,
                           "at": t.created.strftime("%H:%M:%S")} for t in rows]}


@app.get("/api/stats")
def api_stats():
    return stats()


# ------------------------------------------------------------------ WhatsApp webhook

@app.get("/webhook/whatsapp")
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == config.WA_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


@app.post("/webhook/whatsapp")
async def wa_receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401)
    for mid, phone, inc, media in whatsapp.parse(await request.json()):
        if mid in _seen_wa_ids:  # Meta retries deliveries; handle each message once
            continue
        _seen_wa_ids[mid] = None
        while len(_seen_wa_ids) > 5000:
            _seen_wa_ids.popitem(last=False)
        background.add_task(_wa_turn, phone, inc, media)
    return JSONResponse({"ok": True})  # reply fast; work happens in the background


def _wa_turn(phone: str, inc: engine.Incoming, media: str | None):
    if media:
        inc.image = whatsapp.download_media(media)
    run_turn("wa:" + phone, inc, viewer=None)
