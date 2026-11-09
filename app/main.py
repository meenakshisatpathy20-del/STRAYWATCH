"""StrayWatch web server: website, browser chat, public lost posters, WhatsApp webhook, escalation timer."""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections import OrderedDict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import config, engine
from .channels import whatsapp
from .db import Animal, Case, CheckIn, LostPost, Outbox, Photo, SessionLocal, Sighting, Trace, User, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("straywatch")
HERE = Path(__file__).resolve().parent
app = FastAPI(title="StrayWatch")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
_lock = threading.Lock()  # one conversation step at a time keeps SQLite happy and state consistent
_seen_wa_ids: OrderedDict[str, None] = OrderedDict()


# ------------------------------------------------------------------ delivery

def deliver(s, outs: list[engine.Out], viewer: str | None = None) -> list[dict]:
    """Translate, then send each message to its recipient: WhatsApp users via the Cloud API, everyone else to an
    outbox the browser polls. Messages for `viewer` are returned directly."""
    outs = engine.localise(s, outs)
    mine = []
    for o in outs:
        if o.to == viewer:
            mine.append(o.as_dict())
        elif o.to.startswith("wa:") and whatsapp.enabled():
            whatsapp.send(o.to[3:], o)
        else:
            s.add(Outbox(user_id=o.to, payload=o.as_dict()))
    return mine


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


@app.on_event("startup")
async def startup():
    init_db()
    if config.DEMO_MODE:
        from .seed import seed
        await asyncio.to_thread(seed)
    asyncio.create_task(escalation_loop())


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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"stats": stats(), "posts": public_posts(),
                                                              "wa_number": config.WA_DISPLAY_NUMBER})


@app.get("/lost/{code}", response_class=HTMLResponse)
def poster(request: Request, code: str):
    with SessionLocal() as s:
        p = s.scalar(select(LostPost).where(LostPost.code == code.upper()))
        if not p or not p.public:
            raise HTTPException(404, "Poster not found")
        card = post_card(p)
    seen_link = (f"https://wa.me/{config.WA_DISPLAY_NUMBER}?text=SEEN%20{card['code']}" if config.WA_DISPLAY_NUMBER
                 else f"/?seen={card['code']}#try")
    return templates.TemplateResponse(request, "poster.html", {"p": card, "seen_link": seen_link,
                                                               "base": config.PUBLIC_BASE_URL})


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
               photo: UploadFile | None = File(None)):
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
    msgs = await asyncio.to_thread(run_turn, uid, inc, uid)
    return {"messages": msgs, "me": uid}


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
