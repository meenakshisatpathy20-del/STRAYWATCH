"""StrayWatch conversation engine.

Channel-agnostic: WhatsApp and the browser both call `handle()` with an Incoming message and get back a list of
outgoing messages (possibly for several people: the reporter, a caretaker, a volunteer, an owner).
Bot text is written in English here and translated into each recipient's language just before sending.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import ai, config, photos, rules
from .db import (Animal, Case, CaseEvent, CheckIn, LostPost, Sighting, Trace, TranslationCache, User, get_or_create_user, km,
                 nearby, now)

log = logging.getLogger("straywatch.engine")
DATA = Path(__file__).resolve().parent.parent / "data"
FIRST_AID = json.loads((DATA / "first_aid.json").read_text(encoding="utf-8"))
CAMERAS = json.loads((DATA / "cameras.json").read_text(encoding="utf-8"))["cameras"]
SOURCE_NAMES = {"PFA": "People For Animals", "HW": "Humane World for Animals", "BC": "Blue Cross", "WHO": "WHO"}


# ------------------------------------------------------------------ message types

@dataclass
class Incoming:
    text: str = ""
    image: bytes | None = None
    lat: float | None = None
    lon: float | None = None
    button: str | None = None


@dataclass
class Out:
    to: str
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (id, title); max 3 on WhatsApp
    options: list[tuple[str, str]] = field(default_factory=list)  # longer pick-lists (WhatsApp list message)
    image_url: str | None = None
    link: str | None = None
    ask_location: bool = False
    raw: bool = False  # already in the recipient's language (e.g. language picker)

    def as_dict(self):
        return {"to": self.to, "text": self.text, "buttons": [{"id": i, "title": t} for i, t in self.buttons],
                "options": [{"id": i, "title": t} for i, t in self.options], "image_url": self.image_url,
                "link": self.link, "ask_location": self.ask_location}


def photo_url(photo_id: str | None) -> str | None:
    return f"{config.PUBLIC_BASE_URL}/photo/{photo_id}" if photo_id else None


def ist(dt) -> str:
    """Times are stored in UTC; people in India read IST."""
    return (dt + timedelta(hours=5, minutes=30)).strftime("%d %b, %I:%M %p")


def maps_link(lat, lon) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


def step(s: Session, user_id: str, title: str, detail: str):
    """A non-AI decision, written to the same trace as AI calls."""
    s.add(Trace(user_id=user_id, step=title, detail=detail))


# Confidence needed before StrayWatch ACTS on a match by itself (alerting a caretaker first, merging profiles).
# Measured: true matches scored >= 0.95; false matches between look-alike animals scored 0.85-0.9. Lower scores always go to a human to confirm.
AUTO_MATCH = 0.95
ASK_MATCH = 0.6

MENU_BUTTONS = [("m:sos", "Hurt animal: SOS"), ("m:animals", "My animals"), ("m:lost", "Lost & found")]


def menu(u: User, intro: str | None = None) -> Out:
    return Out(u.id, intro or "What would you like to do?\n\nIn a hurry? Just send a photo, I'll figure it out.",
               buttons=MENU_BUTTONS)


def language_picker(u: User) -> Out:
    return Out(u.id, "Namaste! I'm StrayWatch 🐾 I help you look after the animals on your street.\n"
                     "Which language should I use?\nनमस्ते! आप कौन सी भाषा चाहेंगे?",
               options=[(f"lang:{k}", v[1]) for k, v in ai.LANGS.items()], raw=True,
               image_url=f"{config.PUBLIC_BASE_URL}/static/welcome.png")


def first_aid_text(card_id: str, include_bite_rule: bool = True) -> str:
    card = FIRST_AID["cards"].get(card_id) or FIRST_AID["cards"]["general"]
    lines = [f"*{card['title']}: while you wait*"] + [f"{i}. {t}" for i, t in enumerate(card["steps"], 1)]
    srcs = list(card["sources"])
    if include_bite_rule:
        lines.append("\n" + FIRST_AID["bite_rule"])
        srcs += FIRST_AID["bite_sources"]
    lines.append("Sources: " + ", ".join(dict.fromkeys(SOURCE_NAMES[x] for x in srcs)))
    return "\n".join(lines)


def describe_line(p: dict) -> str:
    bits = [p.get("colour"), p.get("markings")]
    if p.get("ear_notch"):
        bits.append("ear notched (likely sterilised)")
    if p.get("collar") and p["collar"].lower() not in ("none", "no", "") and p["collar"].lower() not in " ".join(bits).lower():
        bits.append("collar: " + p["collar"])
    if p.get("ear_tag"):
        bits.append("has an ear tag")
    return ", ".join(b for b in bits if b)


# ------------------------------------------------------------------ entry point

def handle(s: Session, user_id: str, inc: Incoming) -> list[Out]:
    u = get_or_create_user(s, user_id)
    st = copy.deepcopy(u.state or {})
    outs: list[Out] = []
    photo_id = None
    if inc.image:
        try:
            photo_id = photos.save(s, inc.image).id
        except ValueError:
            return [Out(u.id, "Sorry, I couldn't open that file. Please send a normal photo.")]
    if inc.lat is not None and inc.lon is not None:
        st.setdefault("d", {})
        st["d"]["lat"], st["d"]["lon"] = inc.lat, inc.lon
        u.state = st

    b = inc.button or ""
    text = (inc.text or "").strip()

    # 1. Language first.
    if b.startswith("lang:"):
        u.lang = b.split(":", 1)[1] if b.split(":", 1)[1] in ai.LANGS else "en"
        u.state = {}
        return [menu(u, "Great! I'll talk to you in this language. You can change it any time by typing *language*.")]
    if not u.lang:
        u.state = {"pending": {"text": text, "photo": photo_id}} if (text or photo_id) else {}
        return [language_picker(u)]
    if re.fullmatch(r"(?i)\s*(language|bhasha|भाषा)\s*", text):
        return [language_picker(u)]

    # 2. Global commands and buttons.
    if b == "menu" or re.fullmatch(r"(?i)\s*(hi+|hello|hey|namaste|namaskar|start|menu|cancel|stop)\s*[.!]*", text):
        u.state = {}
        return [menu(u)]
    m = re.fullmatch(r"(?i)\s*seen\s+(LF-?\d+)\s*", text)
    if m:
        code = m.group(1).upper().replace("LF", "LF-").replace("--", "-")
        u.state = {"flow": "seen", "d": {"code": code}}
        post = s.scalar(select(LostPost).where(LostPost.code == code))
        name = post.name if post else "the animal"
        return [Out(u.id, f"Thank you for helping {name} get home! 🙏\nPlease send a photo of the animal you saw, "
                          "and share the location where you saw it.", ask_location=True)]
    if re.fullmatch(r"(?i)\s*(volunteer|helper|join)\s*", text) or b == "vol:yes":
        return volunteer_join(s, u)
    if b == "loc:denied":  # the phone refused GPS: never guess a location, ask for a landmark instead
        return [Out(u.id, "I couldn't get your location. 📍\nPlease turn on Location for this site (tap the 🔒 or ⓘ "
                          "next to the web address), or just *type a nearby landmark and area*, e.g. Lalpur Chowk, Ranchi.")]
    if b == "vol:no":
        return [Out(u.id, "No problem. You can say *volunteer* any time. 💛")]
    if b.startswith("case:"):
        return case_button(s, u, b)
    if b.startswith("lm:"):
        return lost_match_button(s, u, b)

    flow = st.get("flow")
    if b == "m:sos":
        u.state = {"flow": "sos", "d": {}}
        return [Out(u.id, "Let's get help. 🚨\n1) Send a photo of the animal (if it's dark, just describe it in words).\n"
                          "2) Share your location.", ask_location=True)]
    if b == "m:animals":
        u.state = {}
        return [Out(u.id, "Your animals 🐶🐮", buttons=[("a:register", "Register an animal"),
                                                      ("a:checkin", "Daily check-in"), ("a:list", "See my animals")])]
    if b == "m:lost":
        u.state = {}
        return [Out(u.id, "Lost & found 🔎", buttons=[("l:lost", "I lost my animal"), ("l:seen", "I saw a lost one"),
                                                    ("l:board", "See lost posters")])]
    if b == "a:register":
        u.state = {"flow": "register", "d": {}}
        return [Out(u.id, "Lovely! 💛 Send a clear photo of the animal you look after. Side-on with the face visible "
                          "works best.")]
    if b == "a:checkin":
        u.state = {"flow": "checkin", "d": {}}
        return [Out(u.id, "Send today's photo. I'll recognise who it is and keep their health diary. 📸")]
    if b == "a:list":
        return [my_animals(s, u)]
    if b == "l:lost":
        u.state = {"flow": "lost", "d": {}}
        return [Out(u.id, "I'm sorry. Let's find them together. 💛\nSend a clear photo of your animal "
                          "(an old photo is fine).")]
    if b == "l:seen":
        u.state = {"flow": "seen", "d": {}}
        return [Out(u.id, "Thank you! Send a photo of the animal you saw and share the location.", ask_location=True)]
    if b == "l:board":
        return [Out(u.id, "All lost posters near you:", link=f"{config.PUBLIC_BASE_URL}/#lost")]

    # 3. Changed their mind mid-chat? An emergency always interrupts, and text that a step isn't waiting for is
    #    checked for a new intent (e.g. "meri billi kho gayi" while reporting a sighting).
    if flow and text and not photo_id and not b:
        expects_text = (flow == "sos" or (flow in ("register", "lost") and st.get("d", {}).get("photo")
                                          and (not st["d"].get("name") or (flow == "lost" and not st["d"].get("when")))))
        urgent = flow != "sos" and re.search(INJURY_WORDS, text.lower())
        if urgent or not expects_text:
            it = ai.intent(s, u.id, text)
            target = {"sos": "m:sos", "lost": "l:lost", "register": "a:register", "checkin": "a:checkin",
                      "seen": "l:seen", "menu": "menu"}.get(it["intent"])
            if target and not (target == "l:seen" and flow == "seen") and not (target == "l:lost" and flow == "lost"):
                step(s, u.id, "Changed topic", f"was '{flow}', now '{it['intent']}'")
                u.state = {}
                if target == "m:sos":
                    u.state = {"flow": "sos", "d": {"text": text}}
                    return sos_step(s, u, "", None, "")
                return handle(s, user_id, Incoming(button=target))

    # 4. Flow steps.
    if flow == "sos":
        return sos_step(s, u, text, photo_id, b)
    if flow == "register":
        return register_step(s, u, text, photo_id, b)
    if flow == "checkin":
        return checkin_step(s, u, text, photo_id, b)
    if flow == "lost":
        return lost_step(s, u, text, photo_id, b)
    if flow == "seen":
        return seen_step(s, u, text, photo_id, b)
    if flow == "photo_choice":
        return photo_choice(s, u, b)
    if flow == "proof":
        return proof_step(s, u, photo_id, b)

    # 5. No flow: a photo on its own, or free text.
    if photo_id:
        return photo_router(s, u, photo_id, text)
    if inc.lat is not None:
        u.lat, u.lon = inc.lat, inc.lon
        return [menu(u, "Thanks, I've saved this as your usual place. 📍")]
    if text:
        it = ai.intent(s, u.id, text)
        step(s, u.id, "Decided what you need", f"intent = {it['intent']} (via {it['via']})")
        if it["intent"] == "sos":
            u.state = {"flow": "sos", "d": {"text": text}}
            return sos_step(s, u, "", None, "")
        if it["intent"] in ("register",):
            return handle(s, user_id, Incoming(button="a:register"))
        if it["intent"] == "checkin":
            return handle(s, user_id, Incoming(button="a:checkin"))
        if it["intent"] == "lost":
            return handle(s, user_id, Incoming(button="l:lost"))
        if it["intent"] == "seen":
            return handle(s, user_id, Incoming(button="l:seen"))
        if it["intent"] == "thanks":
            return [Out(u.id, "Thank you for caring for them. 💛🐾")]
    return [menu(u)]


# ------------------------------------------------------------------ shared helpers

def need_location(u: User) -> Out:
    return Out(u.id, "Please share the location 📍 (in the app tap 📍; on WhatsApp tap 📎 → Location).",
               ask_location=True)


def loc(u: User) -> tuple[float | None, float | None]:
    d = (u.state or {}).get("d", {})
    return d.get("lat"), d.get("lon")


def save_state(u: User, **d):
    st = dict(u.state or {})
    st["d"] = {**st.get("d", {}), **d}
    u.state = st


def photo_router(s, u: User, photo_id: str, text: str) -> list[Out]:
    """A photo with no context: SOS straight away if it looks urgent, otherwise ask what it's for."""
    lat, lon = loc(u)
    desc = ai.describe(s, u.id, photos.data_url(s, photo_id), text)
    if not desc["is_animal"] and desc["confidence"] >= 0.6:
        return [Out(u.id, "I couldn't see an animal in this photo. 🤔 Could you send another one?")]
    if desc["urgency"] == "high" or desc["condition"] in ("injured", "trapped", "dead"):
        step(s, u.id, "Treated as an emergency", f"condition={desc['condition']}, urgency={desc['urgency']}")
        u.state = {"flow": "sos", "d": {"photo": photo_id, "desc": desc, "text": text,
                                        **({"lat": lat, "lon": lon} if lat else {})}}
        return sos_step(s, u, "", None, "")
    u.state = {"flow": "photo_choice", "d": {"photo": photo_id, "desc": desc, **({"lat": lat, "lon": lon} if lat else {})}}
    has_animals = _count_guardian(s, u.id)
    first = ("pc:checkin", "Daily check-in") if has_animals else ("pc:register", "Register it")
    return [Out(u.id, f"I can see: {desc['summary']}\nWhat would you like to do?",
                buttons=[first, ("pc:seen", "Saw a lost one?"), ("pc:sos", "It needs help")])]


def _count_guardian(s, uid) -> int:
    return sum(1 for a in s.scalars(select(Animal)) if uid in (a.guardians or []))


def my_animal_rows(s, uid) -> list[Animal]:
    return [a for a in s.scalars(select(Animal).where(Animal.status == "active")) if uid in (a.guardians or [])]


def photo_choice(s, u, b) -> list[Out]:
    d = (u.state or {}).get("d", {})
    target = {"pc:checkin": "checkin", "pc:register": "register", "pc:seen": "seen", "pc:sos": "sos"}.get(b)
    if not target:
        return [menu(u)]
    u.state = {"flow": target, "d": d}
    fn = {"checkin": checkin_step, "register": register_step, "seen": seen_step, "sos": sos_step}[target]
    return fn(s, u, "", None, "")


def volunteer_join(s, u) -> list[Out]:
    u.is_volunteer = True
    if u.lat is None:
        u.state = {"flow": "volunteer_loc", "d": {}}
        return [Out(u.id, "Thank you for joining as a helper! 🦸 Share your usual location so I only alert you about "
                          "animals nearby. (Or type your area, e.g. *Lalpur, Ranchi*.)", ask_location=True)]
    tip = "" if u.id.startswith("wa:") else ("\n\n🔔 Important: in the app, turn on *notifications* (More → Notifications) "
                                            "so alerts reach you even when StrayWatch is closed.")
    return [Out(u.id, "You're a StrayWatch helper now! 🦸 I'll only alert you about animals within "
                      f"{config.HELPER_RADIUS_KM:g} km, at most {config.MAX_ALERTS_PER_HELPER_PER_DAY} times a day. "
                      "Type *pause* to stop alerts." + tip)]


# ------------------------------------------------------------------ RESCUE SOS

LOCATION_ASK = ("Got it. Now please share the location 📍 so help can find the animal.\n"
                "Can't share it? Type a nearby landmark and area, e.g. *Lalpur Chowk, Ranchi*.")


def sos_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"] = photo_id
        d.pop("desc", None)
    # Confirming a typed landmark
    if b == "sos:locyes" and d.get("cand"):
        d["lat"], d["lon"] = d["cand"]["lat"], d["cand"]["lon"]
        d["place"] = d.pop("cand")["label"]
    elif b == "sos:locno":
        d.pop("cand", None)
        u.state = {"flow": "sos", "d": d}
        return [Out(u.id, "Okay. Please type another landmark with the area name, or share your location 📍.",
                    ask_location=True)]
    elif text and d.get("asked_loc") and d.get("lat") is None and not re.search(INJURY_WORDS, text.lower()):
        # They were asked where it is, and typed a place: look it up, and confirm before using it.
        from . import geo
        hit = geo.geocode(text, (u.lat, u.lon) if u.lat else None)
        if not hit:
            u.state = {"flow": "sos", "d": d}
            return [Out(u.id, f"I couldn't find “{text[:60]}” on the map. 🙏 Please add the area or city name "
                              "(e.g. *Ratu Road, Ranchi*), or share your location 📍.", ask_location=True)]
        d["cand"] = hit
        u.state = {"flow": "sos", "d": d}
        return [Out(u.id, f"Is it near *{hit['label']}*?", link=maps_link(hit["lat"], hit["lon"]),
                    buttons=[("sos:locyes", "Yes, send help"), ("sos:locno", "No, somewhere else")])]
    elif text:
        d["text"] = (d.get("text", "") + " " + text).strip()
    u.state = {"flow": "sos", "d": d}
    if not d.get("photo") and not d.get("text"):
        return [Out(u.id, "Please send a photo of the animal, or describe it in words if it's dark.")]
    if d.get("lat") is None:
        if u.lat is not None and b == "sos:here":
            d["lat"], d["lon"] = u.lat, u.lon
        else:
            d["asked_loc"] = True
            u.state = {"flow": "sos", "d": d}
            return [Out(u.id, LOCATION_ASK, ask_location=True)]
    return create_case(s, u, d)


RANK = {"LOW": 0, "CHECK": 1, "HIGH": 2}
LEVEL_WORDS = {"HIGH": "URGENT", "CHECK": "Needs a check", "LOW": "Logged, no rescue needed"}
TEAM_RADIUS_KM = {"volunteer": None, "caretaker": None}  # None = config.HELPER_RADIUS_KM; teams cover the city
CITY_RADIUS_KM = 20.0


def ledger(s, case: Case, agent: str, kind: str, detail: str, public: bool = True, trace_title: str | None = None):
    """Write one entry to the case ledger (and to the reporter's live trace on the website)."""
    s.add(CaseEvent(case_id=case.id, agent=agent, kind=kind, detail=detail[:1000], public=public))
    if trace_title:
        step(s, case.reporter_id, trace_title, detail)


def track_link(case: Case) -> str:
    return f"{config.PUBLIC_BASE_URL}/track/SW-{case.id}?k={case.track_key}"


def hop_seconds(case: Case) -> int:
    """Seconds each responder gets. Set by the rules (3 min HIGH, 10 min CHECK); config can shorten it for demos."""
    rule = (case.hop_minutes or 3) * 60
    return min(rule, config.ESCALATE_AFTER_SECONDS) if config.DEMO_MODE else rule


def hop_words(case: Case) -> str:
    sec = hop_seconds(case)
    return f"{sec // 60} minutes" if sec >= 120 else (f"{sec} seconds" if sec < 60 else "1 minute")


def wait_words() -> str:
    sec = config.ESCALATE_AFTER_SECONDS
    return f"{sec // 60} minutes" if sec >= 120 else (f"{sec} seconds" if sec < 60 else "1 minute")


def create_case(s, u: User, d: dict, source: str = "citizen") -> list[Out]:
    lat, lon = d["lat"], d["lon"]
    img = photos.data_url(s, d.get("photo"))
    desc = d.get("desc") or ai.describe(s, u.id, img, d.get("text", ""))
    said_hurt = bool(re.search(INJURY_WORDS, (d.get("text") or "").lower()))
    desc = apply_reporter_words(s, u.id, desc, d.get("text", ""))
    u.state = {}
    if img and not desc["is_animal"] and desc["confidence"] >= 0.7:
        step(s, u.id, "Report not sent", "No animal visible in the photo, so no helper was disturbed.")
        u.trust = (u.trust or 0) - 1
        return [Out(u.id, "I couldn't see an animal in this photo, so I haven't alerted anyone. If it's dark, "
                          "please describe the animal in words and share the location again.")]

    # Same animal already reported nearby in the last 60 minutes? Merge instead of sending a second rescue.
    recent = [c for dist, c in nearby(s, Case, lat, lon, 0.2)
              if c.status in ("alerting", "assigned") and c.created > now() - timedelta(minutes=60)
              and (c.triage or {}).get("species") == desc["species"]]
    if recent:
        c = recent[0]
        s.info["last_case_id"] = c.id
        c.merged_reports = (c.merged_reports or 1) + 1
        ledger(s, c, "Triage", "merged", ("The Camera agent" if source == "camera" else "Another person") +
               f" reported the same animal ({c.merged_reports} reports). No second rescue was started.")
        new = rules.decide(rules.facts_from(desc, said_hurt, now()))
        if RANK.get(new.level, 0) > RANK.get(c.level or "LOW", 0):
            c.level, c.reasons, c.hop_minutes = new.level, new.reasons, new.hop_minutes
            c.teams = list(dict.fromkeys(new.teams + list(c.teams or [])))
            extra = pick_responders(s, c.lat, c.lon, new.teams, exclude={c.reporter_id, *(c.asked or []), *(c.queue or [])})
            c.queue = extra + list(c.queue or [])
            ledger(s, c, "Rules", "upgraded", f"New report is more urgent → {new.level}: " + "; ".join(new.reasons),
                   trace_title="Rules raised urgency")
            if c.status == "alerting" and extra:
                c.alerted_at = now() - timedelta(days=1)  # Watch agent moves to the right team on its next tick
        step(s, u.id, "Merged with an open case", f"SW-{c.id} already open ({c.merged_reports} reports), no duplicate rescue")
        if d.get("photo"):
            add_sighting(s, u.id, d["photo"], desc, lat, lon, "sos")
        msg = "Someone already reported this animal and help is " + (
            "on the way. 🙏" if c.status == "assigned" else "being alerted right now. 🙏")
        return [Out(u.id, f"*Case SW-{c.id}*\n{msg}\nThank you for looking out for them.", link=track_link(c)),
                Out(u.id, first_aid_text(desc["first_aid_card"]))]

    recent_mine = s.scalar(select(func.count()).select_from(Case).where(
        Case.reporter_id == u.id, Case.created > now() - timedelta(hours=1))) or 0
    if source == "citizen" and recent_mine >= config.MAX_SOS_PER_HOUR:
        step(s, u.id, "Report limit", f"{recent_mine} reports in the last hour; not dispatched.")
        return [Out(u.id, f"You've sent {recent_mine} reports in the last hour, so I've paused new ones for a while to "
                          "keep responders free for emergencies. 🙏 If this one is urgent, call the animal ambulance on "
                          "*1962*.")]
    dec = rules.decide(rules.facts_from(desc, said_hurt, now()))
    case = Case(reporter_id=u.id, photo_id=d.get("photo"), triage=desc, lat=lat, lon=lon,
                status="alerting" if dec.dispatch else "logged", level=dec.level, reasons=dec.reasons,
                teams=dec.teams, hop_minutes=dec.hop_minutes, source=source, track_key=secrets.token_urlsafe(8)[:10])
    s.add(case)
    s.flush()
    s.info["last_case_id"] = case.id
    if source == "camera":
        ledger(s, case, "Camera", "reported", (d.get("camera_note") or "Camera agent flagged an animal in a lane.") + ".")
    else:
        ledger(s, case, "Person", "reported",
               f"A citizen reported a {desc['species']}" + (" with a photo" if d.get("photo") else " in words") + ".")
    ledger(s, case, "Triage", "triaged", f"{desc['summary']} (condition: {desc['condition']}, on road: "
                                         f"{'yes' if desc.get('on_road') else 'no'})")
    ledger(s, case, "Rules", "ruled", f"{dec.level}: " + "; ".join(dec.reasons), trace_title="Rules set urgency")

    if d.get("photo") and not dec.dispatch:
        add_sighting(s, u.id, d["photo"], desc, lat, lon, "sos")
    if not dec.dispatch:
        ledger(s, case, "Hotspot", "logged", "Added to the hotspot map. No responder disturbed.")
        return [Out(u.id, f"*Case SW-{case.id} · {LEVEL_WORDS['LOW']}*\n{desc['summary']}\n\nIt is off the road and "
                          "doesn't look hurt, so I haven't called a rescuer. I've added it to the hotspot map so the "
                          "city can see where animals gather. Thank you! 💛\nIf that changes, tap below.",
                    buttons=[(f"case:worse:{case.id}", "It's hurt / on road"), ("menu", "Menu")], link=track_link(case))]

    # Is this a known animal? Then its caretaker hears first.
    animal = None
    if img and desc["species"] != "other":
        cands = [a for dist, a in nearby(s, Animal, lat, lon, config.REID_RADIUS_KM, species=desc["species"])
                 if a.status == "active" and a.photo_id][:5]
        if cands:
            mres = ai.match(s, u.id, photos.raw(s, d.get("photo")), [(a.id, photos.raw(s, a.photo_id), f"{a.name}: {describe_line(a.profile)}")
                                            for a in cands], "rescue: is the hurt animal one of these registered animals?")
            if mres["key"] and mres["confidence"] >= AUTO_MATCH:
                animal = s.get(Animal, mres["key"])
                case.animal_id = animal.id
                step(s, u.id, "Recognised the animal", f"{animal.name} ({mres['confidence']:.0%}): {mres['reason']}")
                ledger(s, case, "Identify", "identified", f"Recognised {animal.name}, a registered {animal.species} "
                                                          f"({mres['confidence']:.0%} sure). Caretaker goes first.")
    caretakers = [g for g in (animal.guardians if animal else []) if g != u.id]
    responders = pick_responders(s, lat, lon, dec.teams, exclude={u.id, *caretakers})
    case.queue = caretakers + responders
    plan = (["caretaker"] if caretakers else []) + dec.teams
    ledger(s, case, "Dispatch", "planned", "Order: " + " → ".join(rules.TEAM_LABELS[t] for t in plan) +
           f" → duty coordinator. {len(case.queue)} responder(s) found; each gets {hop_words(case)}.",
           trace_title="Planned who to ask")
    if d.get("photo"):
        # A recognised registered animal is not someone's lost pet: don't send its photo to lost-poster matching.
        add_sighting(s, u.id, d["photo"], desc, lat, lon, "sos", check=animal is None)

    intro = f"*Case SW-{case.id} · {LEVEL_WORDS[dec.level]}*\n{desc['summary']}\nWhy: {'; '.join(dec.reasons)}."
    if animal:
        intro += f"\n\nThis may be *{animal.name}*, a registered community animal. 💛 Their caretaker lives close by, so I'm telling them first."
    alerts = alert_next(s, case)
    if not animal and case.current_helper:
        team, _ = responder_label(s, case, case.current_helper)
        intro += f"\n\nI've alerted the nearest {rules.TEAM_LABELS.get(team, 'helper')}."
    if case.status == "alerting":
        intro += "\nYou can follow every step here:"
    gone = [(f"case:cancel:{case.id}", "Animal has gone")] if case.status == "alerting" else []
    outs = [Out(u.id, intro, link=track_link(case), buttons=gone), Out(u.id, first_aid_text(desc["first_aid_card"]))]
    return outs + alerts


INJURY_WORDS = r"(bleed|blood|khoon|injur|hurt|ghayal|chot|wound|fracture|limp|broken|can'?t walk|not moving|accident|hit by|takkar|gaadi ne|maar|trapped|stuck|phans|fasa|dying|unconscious)"
ROAD_WORDS = r"(road|sadak|highway|accident|hit by|takkar|gaadi|vehicle|truck|bike|car)"


def apply_reporter_words(s, uid, desc: dict, text: str) -> dict:
    """A rescue tool must believe the person on the spot about what a photo can't show (a wound on the far side,
    a dark night). Code-side rules, applied after the AI, so the AI can never talk an injury down."""
    t = (text or "").lower()
    if not t:
        return desc
    d = dict(desc)
    hurt = re.search(INJURY_WORDS, t)
    road = re.search(ROAD_WORDS, t)
    if hurt:
        if d["condition"] in ("healthy", "unknown"):
            d["condition"] = "injured"
        d["urgency"] = "high"
        d["first_aid_card"] = "road_injury" if road else ("trapped" if re.search(r"(trapped|stuck|phans|fasa)", t) else "bleeding")
        if "no visible injur" in d["summary"].lower() or "despite" in d["summary"].lower():
            d["summary"] = (f"{d['species'].title()} reported injured by the person on the spot "
                            f"({text.strip()[:80]}). Injury not clearly visible in the photo.")
        step(s, uid, "Trusted the reporter", "They described an injury, so treated as urgent even if the photo "
                                             "doesn't show it clearly.")
    elif road and d["species"] in ("cow", "buffalo") and d["first_aid_card"] == "general":
        d["first_aid_card"] = "cattle_on_road"
    if road and not d.get("on_road"):
        d["on_road"] = True
    return d


def _alerts_today(s, uid: str) -> int:
    since = now() - timedelta(days=1)
    return sum(1 for c in s.scalars(select(Case).where(Case.created >= since)) if uid in (c.asked or []))


def pick_helpers(s, lat, lon, exclude: set) -> list[str]:
    """Nearby volunteers (8 km), skipping anyone paused or already at the daily cap."""
    out = []
    for dist, h in nearby(s, User, lat, lon, config.HELPER_RADIUS_KM, is_volunteer=True):
        if h.id in exclude or (h.state or {}).get("paused") or (h.role and h.role != "volunteer"):
            continue
        if _alerts_today(s, h.id) >= config.MAX_ALERTS_PER_HELPER_PER_DAY:
            continue
        out.append(h.id)
    return out[:8]


def pick_responders(s, lat, lon, teams: list[str], exclude: set) -> list[str]:
    """Up to 2 nearest people per team, in the order the rules chose. Teams cover the city; volunteers 8 km."""
    out: list[str] = []
    for team in teams:
        if team == "volunteer":
            ids = pick_helpers(s, lat, lon, exclude | set(out))[:3]
        else:
            ids = [h.id for dist, h in nearby(s, User, lat, lon, CITY_RADIUS_KM, role=team)
                   if h.id not in exclude and h.id not in out and not (h.state or {}).get("paused")][:2]
        out += ids
    return out


def coordinators(s, case: Case) -> list[str]:
    return [h.id for dist, h in nearby(s, User, case.lat, case.lon, CITY_RADIUS_KM * 3, role="coordinator")
            if h.id not in (case.asked or [])]


def responder_label(s, case: Case, uid: str) -> tuple[str, bool]:
    """('caretaker'|team, is_caretaker) for a responder on this case."""
    if case.animal_id:
        a = s.get(Animal, case.animal_id)
        if a and uid in (a.guardians or []):
            return "caretaker", True
    h = s.get(User, uid)
    return (h.role if h and h.role else "volunteer"), False


def alert_next(s, case: Case) -> list[Out]:
    q = list(case.queue or [])
    if not q and case.status == "alerting":
        coord = coordinators(s, case)
        if coord:
            q = coord[:1]
            ledger(s, case, "Watch", "escalated", "No responder accepted. Escalated to the duty coordinator.",
                   trace_title="Escalated to coordinator")
    while q:
        hid = q.pop(0)
        h = s.get(User, hid)
        if not h:
            continue
        case.queue, case.current_helper, case.alerted_at = q, hid, now()
        case.asked = list(case.asked or []) + [hid]
        team, is_caretaker = responder_label(s, case, hid)
        name = s.get(Animal, case.animal_id).name if is_caretaker else "an animal"
        dist = km(h.lat, h.lon, case.lat, case.lon)
        where = f"{dist:.1f} km from you" if dist != float("inf") else "near you"
        tri = case.triage or {}
        level = LEVEL_WORDS.get(case.level or "HIGH", "URGENT")
        if is_caretaker:
            head = f"*This may be {name}. They need you!* 🚨 (please check the photo)"
        elif team == "coordinator":
            head = f"*Duty coordinator: case SW-{case.id} needs an owner* ⚠️ {len(case.asked) - 1} responder(s) didn't take it."
        else:
            head = f"*{level}: an animal near you needs help* 🚨"
        away = f"{dist:.1f} km away" if dist != float("inf") else "nearby"
        ledger(s, case, "Dispatch", "alerted", f"Alerted {'the caretaker' if is_caretaker else rules.TEAM_LABELS.get(team, team)} "
                                               f"(responder #{len(case.asked)}, {away}). Waiting {hop_words(case)}.",
               trace_title="Alerted a " + ("caretaker" if is_caretaker else rules.TEAM_LABELS.get(team, "helper")))
        return [Out(hid, f"{head}\nCase SW-{case.id}: {tri.get('summary', '')}\nWhere: {where}\n"
                         f"If you can't reply in {hop_words(case)}, I'll ask the next one.",
                    image_url=photo_url(case.photo_id),
                    buttons=[(f"case:go:{case.id}", "On my way"), (f"case:no:{case.id}", "Can't make it")])]
    case.queue, case.current_helper = [], None
    if case.status == "alerting":
        case.status = "unanswered"
        ledger(s, case, "Watch", "unanswered", "Everyone was asked, including the duty coordinator. Case kept open; "
                                               "new responders nearby will be alerted.", trace_title="No responder free")
        return [Out(case.reporter_id,
                    f"I'm sorry, no StrayWatch responder near you is free right now (case SW-{case.id}). 🙏\n"
                    "Please call the government animal ambulance on *1962* or your municipal animal helpline. "
                    "If a person is hurt, call 112.\nI'll keep this case open and alert new responders who join nearby.")]
    return []


def escalate_due(s) -> list[Out]:
    """The Watch agent, every ~20 s: anyone who didn't answer in their time is skipped."""
    outs = []
    for c in s.scalars(select(Case).where(Case.status == "alerting", Case.alerted_at.is_not(None))):
        if c.alerted_at > now() - timedelta(seconds=hop_seconds(c)):
            continue
        ledger(s, c, "Watch", "timeout", f"No reply in {hop_words(c)}. Moving on.", trace_title="No reply in time")
        nxt = alert_next(s, c)
        if c.status == "alerting":  # keep the person on the roadside informed, once per hop
            outs.append(Out(c.reporter_id, f"⏳ Still finding help for case SW-{c.id}. The last person couldn't reply, "
                                           f"so I've asked responder #{len(c.asked)}. Please stay safe."))
        outs += nxt
    return outs


def case_button(s, u: User, b: str) -> list[Out]:
    _, action, cid = b.split(":")
    c = s.get(Case, int(cid))
    if not c:
        return [Out(u.id, "That case was not found.")]
    if action == "go":
        if c.assigned_to and c.assigned_to != u.id:
            return [Out(u.id, "Thank you! Someone else is already on the way. 💛")]
        if c.status == "closed":
            return [Out(u.id, "This case is already closed. Thank you! 💛")]
        c.assigned_to, c.accepted_at, c.status, c.current_helper = u.id, now(), "assigned", u.id
        mins = (c.accepted_at - c.created).total_seconds() / 60
        team, is_ct = responder_label(s, c, u.id)
        ledger(s, c, "Person", "accepted", f"{'The caretaker' if is_ct else rules.TEAM_LABELS.get(team, team).capitalize()} "
                                           f"accepted after {mins:.1f} min.", trace_title="Helper accepted")
        who = "The caretaker" if is_ct else ("A " + rules.TEAM_LABELS.get(team, "StrayWatch helper"))
        return [
            Out(u.id, f"Thank you! 🙏 The person who found it is waiting nearby.\nCase SW-{c.id}",
                link=maps_link(c.lat, c.lon), buttons=[(f"case:safe:{c.id}", "It's safe now"),
                                                      (f"case:more:{c.id}", "Need more help")]),
            Out(u.id, first_aid_text((c.triage or {}).get("first_aid_card", "general"), include_bite_rule=False)),
            Out(c.reporter_id, f"✅ {who} is on the way! (case SW-{c.id})\nPlease stay at a safe distance until they arrive."),
        ]
    if action == "no":
        if c.current_helper != u.id:
            return [Out(u.id, "No problem, thank you. 💛")]
        ledger(s, c, "Person", "declined", "Responder can't go. Asking the next one.", trace_title="Helper can't go")
        nxt = alert_next(s, c)
        upd = [Out(c.reporter_id, f"⏳ Still finding help for case SW-{c.id}: asking responder #{len(c.asked)}.")] \
            if c.status == "alerting" else []
        return [Out(u.id, "No problem, I'll ask someone else. 💛")] + upd + nxt
    if action == "safe":
        if c.status == "closed":
            return [Out(u.id, "This case is already closed. Thank you! 💛")]
        u.state = {"flow": "proof", "d": {"case": c.id}}
        return [Out(u.id, f"Great work! 🙏 To close case SW-{c.id}, please send one photo of the animal now "
                          "(off the road, at the clinic, or being helped). The reporter will see it.",
                    buttons=[(f"case:nophoto:{c.id}", "Can't take a photo")])]
    if action == "nophoto":
        u.state = {}
        return close_case(s, c, u, None, "Closed without a photo (responder couldn't take one).")
    if action == "cancel":
        if c.reporter_id != u.id or c.status not in ("alerting", "assigned", "unanswered"):
            return [Out(u.id, "Thanks for letting me know. 💛")]
        busy = c.assigned_to or c.current_helper
        c.status, c.closed_at, c.queue, c.current_helper = "cancelled", now(), [], None
        ledger(s, c, "Person", "cancelled", "The reporter says the animal has gone or it was a false alarm. "
                                            "Responders were told to stand down.", trace_title="Case cancelled")
        outs = [Out(u.id, f"Okay, I've closed case SW-{c.id} and told the responder. Thank you for caring. 💛")]
        if busy:
            outs.append(Out(busy, f"ℹ️ Case SW-{c.id} is cancelled: the reporter says the animal has gone. "
                                  "No need to go. Thank you! 🙏"))
        return outs
    if action == "worse":
        if c.reporter_id != u.id or c.status != "logged":
            return [Out(u.id, "Thanks, this case is already being handled. 💛")]
        desc = dict(c.triage or {})
        desc["condition"] = "injured" if desc.get("condition") in (None, "healthy", "unknown") else desc["condition"]
        dec = rules.decide(rules.facts_from(desc, True, now()))
        c.level, c.reasons, c.teams, c.hop_minutes, c.status = dec.level, dec.reasons, dec.teams, dec.hop_minutes, "alerting"
        ledger(s, c, "Rules", "ruled", f"Reporter says it got worse → {dec.level}: " + "; ".join(dec.reasons),
               trace_title="Rules set urgency")
        c.queue = pick_responders(s, c.lat, c.lon, dec.teams, exclude={u.id})
        return [Out(u.id, f"Thank you for telling me. 🚨 Case SW-{c.id} is now *{LEVEL_WORDS[dec.level]}*. "
                          "I'm alerting responders now.", link=track_link(c))] + alert_next(s, c)
    if action == "more":
        c.queue = pick_responders(s, c.lat, c.lon, (c.teams or ["volunteer"]),
                                  exclude={c.reporter_id, *(c.asked or [])})
        ledger(s, c, "Person", "more_help", "Responder asked for one more person.", trace_title="Extra help requested")
        nxt = alert_next(s, c) if c.queue else []
        c.status = "assigned"
        return [Out(u.id, "I'm asking one more responder nearby." if nxt else
                    "No other responder is free nearby. Please call 1962 (animal ambulance) or the municipal helpline.")] + nxt
    return []


def proof_step(s, u: User, photo_id: str | None, b: str) -> list[Out]:
    """A responder closing a case sends a photo; the AI checks it shows the animal safe."""
    c = s.get(Case, ((u.state or {}).get("d") or {}).get("case", 0))
    if not c or c.status == "closed":
        u.state = {}
        return [menu(u)]
    if not photo_id:
        return [Out(u.id, f"Please send a photo of the animal to close case SW-{c.id}.",
                    buttons=[(f"case:nophoto:{c.id}", "Can't take a photo")])]
    chk = ai.proof_check(s, c.reporter_id, photos.data_url(s, c.photo_id), photos.data_url(s, photo_id),
                         (c.triage or {}).get("species", "animal"))
    if not chk["ok"]:
        ledger(s, c, "Dispatch", "proof_rejected", f"Proof photo not accepted: {chk['note']}", public=False)
        return [Out(u.id, f"I couldn't see the animal safe in that photo ({chk['note']}). Please try another, "
                          "or tap below.", buttons=[(f"case:nophoto:{c.id}", "Can't take a photo")])]
    u.state = {}
    if chk.get("same_animal") is False:
        ledger(s, c, "Dispatch", "proof_review", "Proof photo may show a different animal: flagged for the duty "
                                                 "coordinator to review.", public=False)
    return close_case(s, c, u, photo_id, chk["note"] if chk["checked"] else "Proof photo saved.")


def close_case(s, c: Case, u: User, proof_photo: str | None, note: str) -> list[Out]:
    c.status, c.closed_at, c.proof_photo_id, c.proof_note = "closed", now(), proof_photo, note
    rep = s.get(User, c.reporter_id)
    if rep:
        rep.trust = (rep.trust or 0) + 1
    mins = (c.closed_at - c.created).total_seconds() / 60
    ledger(s, c, "Dispatch", "closed", f"Closed after {mins:.0f} min. " + ("Proof photo checked by AI: " if proof_photo else "")
           + note, trace_title="Case closed")
    if c.animal_id:
        s.add(CheckIn(animal_id=c.animal_id, user_id=u.id, photo_id=proof_photo, note=f"Rescued (case SW-{c.id})", concern=True))
    if c.photo_id or proof_photo:
        add_sighting(s, u.id, proof_photo or c.photo_id, c.triage or {}, c.lat, c.lon, "rescue", check=False)
    return [Out(u.id, "You're a hero! 🦸💛 Case closed. If the animal needs a vet, call 1962 or take it to the nearest clinic."),
            Out(c.reporter_id, f"🎉 Good news! The animal from case SW-{c.id} is safe now. Thank you for caring.",
                image_url=photo_url(proof_photo), link=track_link(c))]


# ------------------------------------------------------------------ CAMERA AGENT

def camera_case(s, cam_id: str, ev: dict, image: bytes) -> tuple[Case | None, list[Out]]:
    """An edge Camera agent saw an animal in a lane too long. The geometry is fact (code); the AI reads the snapshot."""
    cam = CAMERAS[cam_id]
    u = get_or_create_user(s, f"camera:{cam_id}")
    u.lang, u.name, u.lat, u.lon = "en", cam["name"], cam["lat"], cam["lon"]
    pid = photos.save(s, image).id
    secs = ev.get("seconds_in_lane", 0)
    kind = ev.get("kind", "dwell")
    note = (f"{cam['name']}: a {ev.get('species', 'animal')} has " +
            (f"not moved for {secs:.0f} s in a traffic lane" if kind == "still" else f"stayed {secs:.0f} s in a traffic lane"))
    desc = ai.describe(s, u.id, photos.data_url(s, pid), "Road camera alert. " + note)
    if not desc["is_animal"] or desc["species"] == "other":
        desc["is_animal"], desc["species"] = True, ev.get("species", "cow")
    # Camera geometry is measured, not guessed: the animal IS in the lane.
    desc.update(on_road=True, blocking_lane=True, road_type=cam.get("road_type", "unknown"))
    if kind == "still":
        desc["lying_still"] = True
        if desc["condition"] == "healthy":
            desc["condition"] = "unknown"
    if desc["species"] in ("cow", "buffalo") and desc["first_aid_card"] == "general":
        desc["first_aid_card"] = "cattle_on_road"
    s.info.pop("last_case_id", None)
    outs = create_case(s, u, {"photo": pid, "desc": desc, "lat": cam["lat"], "lon": cam["lon"], "camera_note": note},
                       source="camera")
    cid = s.info.pop("last_case_id", None)
    return (s.get(Case, cid) if cid else None), [o for o in outs if not o.to.startswith("camera:")]


PLACES = [("Ratu Road", 23.3905, 85.3050), ("Lalpur", 23.3725, 85.3330), ("Kanke Road", 23.3950, 85.3200),
          ("Hatia", 23.3300, 85.3000), ("Main Road, Ranchi", 23.3600, 85.3300), ("Doranda", 23.3450, 85.3200)]


def place_name(lat, lon) -> str:
    best = min(PLACES, key=lambda p: km(lat, lon, p[1], p[2]))
    return best[0] if km(lat, lon, best[1], best[2]) <= 1.5 else f"area {lat:.2f}, {lon:.2f}"


def hotspot_actions(s, days: int = 30) -> list[dict]:
    """Hotspot agent: repeat cattle-on-road reports at the same place and time of day become a concrete action."""
    since = now() - timedelta(days=days)
    rows = []
    for x in s.scalars(select(Sighting).where(Sighting.created >= since)):
        if x.species in rules.CATTLE and (x.profile or {}).get("on_road"):
            rows.append((x.lat, x.lon, x.created))
    for c in s.scalars(select(Case).where(Case.created >= since)):
        if (c.triage or {}).get("species") in rules.CATTLE and (c.triage or {}).get("on_road"):
            rows.append((c.lat, c.lon, c.created))
    groups: dict[tuple, int] = {}
    for lat, lon, at in rows:
        hour = rules.ist_hour(at) // 2 * 2  # 2-hour windows
        key = (place_name(lat, lon), hour)
        groups[key] = groups.get(key, 0) + 1
    out = []
    for (place, hour), n in sorted(groups.items(), key=lambda kv: -kv[1]):
        if n < 2:
            continue
        h12 = lambda x: f"{(x - 1) % 12 + 1} {'AM' if x < 12 or x == 24 else 'PM'}"
        out.append({"place": place, "hour": hour, "count": n,
                    "action": f"Send the municipal cattle team to {place} around {h12(hour)}",
                    "why": f"{n} cattle-on-road reports between {h12(hour)} and {h12(hour + 2)} in the last {days} days"})
    return out[:6]


def signboards(s) -> list[dict]:
    """Driver warnings for roadside signboards: no app needed (deck slide 2). One entry per camera."""
    out = []
    for cam_id, cam in CAMERAS.items():
        open_cases = [c for d, c in nearby(s, Case, cam["lat"], cam["lon"], 0.5)
                      if c.status in ("alerting", "assigned", "unanswered") and c.created > now() - timedelta(minutes=45)]
        cattle = [c for c in open_cases if (c.triage or {}).get("species") in rules.CATTLE]
        dogs = [c for c in open_cases if (c.triage or {}).get("species") == "dog"]
        msg = ("CATTLE ON ROAD AHEAD · SLOW DOWN" if cattle else
               "ANIMAL ON ROAD AHEAD · SLOW DOWN" if dogs or open_cases else "")
        out.append({"camera_id": cam_id, "name": cam["name"], "lat": cam["lat"], "lon": cam["lon"],
                    "show": bool(msg), "message_en": msg,
                    "message_hi": ("आगे सड़क पर पशु · धीरे चलें" if msg else ""),
                    "cases": [f"SW-{c.id}" for c in open_cases]})
    return out


# ------------------------------------------------------------------ MY ANIMALS

def register_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"], d["desc"] = photo_id, None
    if b == "reg:retake":
        u.state = {"flow": "register", "d": {}}
        return [Out(u.id, "Okay, send a new photo. 📸")]
    if not d.get("photo"):
        u.state = {"flow": "register", "d": d}
        return [Out(u.id, "Please send a photo of the animal first. 📸")]
    if not d.get("desc"):
        d["desc"] = ai.describe(s, u.id, photos.data_url(s, d["photo"]), text)
        if not d["desc"]["is_animal"] and d["desc"]["confidence"] >= 0.6:
            u.state = {"flow": "register", "d": {}}
            return [Out(u.id, "I couldn't see an animal in that photo. Please try another one. 📸")]
        u.state = {"flow": "register", "d": d}
        return [Out(u.id, f"What a lovely {d['desc']['species']}! 😍 What's their name?")]
    if not d.get("name"):
        if not text:
            return [Out(u.id, "What's their name? (Any name you call them is fine.)")]
        d["name"] = text.strip()[:40].title()
        u.state = {"flow": "register", "d": d}
    if d.get("lat") is None:
        if b == "reg:here" and u.lat is not None:
            d["lat"], d["lon"] = u.lat, u.lon
        else:
            u.state = {"flow": "register", "d": d}
            btns = [("reg:here", "Same as my place")] if u.lat is not None else []
            return [Out(u.id, f"Where does {d['name']} usually stay? Share the location 📍 "
                              "(only you and verified helpers can see the exact spot).", ask_location=True, buttons=btns)]
    p = d["desc"]
    # Already registered by someone else? Join as an extra caretaker instead of creating a duplicate.
    if not d.get("dup_checked"):
        d["dup_checked"] = True
        u.state = {"flow": "register", "d": d}
        cands = [a for dist, a in nearby(s, Animal, d["lat"], d["lon"], 0.5, species=p["species"])
                 if a.photo_id and u.id not in (a.guardians or [])][:5]
        if cands:
            mres = ai.match(s, u.id, photos.raw(s, d["photo"]),
                            [(a.id, photos.raw(s, a.photo_id), f"{a.name}: {describe_line(a.profile)}") for a in cands],
                            "registration: is this animal already registered by another caretaker?")
            if mres["key"] and mres["confidence"] >= AUTO_MATCH:
                a = s.get(Animal, mres["key"])
                d["dup_of"] = a.id
                u.state = {"flow": "register", "d": d}
                return [Out(u.id, f"This looks like *{a.name}*, who already has a caretaker nearby. 💛 "
                                  "Add yourself as another caretaker?",
                            buttons=[("reg:join", "Yes, add me"), ("reg:new", "No, different one")])]
    if b == "reg:join" and d.get("dup_of"):
        a = s.get(Animal, d["dup_of"])
        a.guardians = list(dict.fromkeys((a.guardians or []) + [u.id]))
        u.state = {}
        return [Out(u.id, f"Done! You're now one of {a.name}'s caretakers. 💛 Send a photo any day for a check-in.")]
    if not d.get("confirmed"):
        if b == "reg:ok":
            d["confirmed"] = True
        else:
            u.state = {"flow": "register", "d": d}
            who = ", ".join(x for x in (p["species"], p["sex_guess"], p["age_guess"]) if x and x != "unknown")
            card = (f"*Say hi to {d['name']}!* 💛\nAnimal: {who}\n"
                    f"Looks: {describe_line(p) or 'n/a'}\nHealth: {p['injuries'] if p['condition'] != 'healthy' else 'looks well'}")
            if p["species"] == "dog":
                card += "\nEar notch: " + ("yes, likely sterilised ✂️" if p["ear_notch"] else "not seen")
            return [Out(u.id, card, image_url=photo_url(d["photo"]),
                        buttons=[("reg:ok", "Looks right"), ("reg:retake", "Retake photo")])]
    a = Animal(name=d["name"], species=p["species"], profile=p, photo_id=d["photo"], lat=d["lat"], lon=d["lon"],
               guardians=[u.id])
    s.add(a)
    s.flush()
    if u.lat is None:
        u.lat, u.lon = d["lat"], d["lon"]
    add_sighting(s, u.id, d["photo"], p, d["lat"], d["lon"], "register")
    step(s, u.id, "Registered an animal", f"{a.name} the {a.species}")
    u.state = {"flow": "vax", "d": {"animal": a.id}}
    return [Out(u.id, f"🎉 {a.name} is registered! Has {a.name} had an anti-rabies vaccine in the last year?",
                buttons=[("vax:yes", "Yes"), ("vax:no", "Not yet"), ("vax:unknown", "Don't know")])]


def vax_answer(s, u, b) -> list[Out]:
    a = s.get(Animal, ((u.state or {}).get("d") or {}).get("animal", 0))
    u.state = {}
    if not a:
        return [menu(u)]
    a.rabies_vaccinated_on = now().date().isoformat() if b == "vax:yes" else ("unknown" if b == "vax:unknown" else None)
    tip = ("Great! I'll remind you when the next one is due. 💉" if b == "vax:yes" else
           "A yearly anti-rabies vaccine keeps them and your neighbours safe. Many municipal and NGO drives do it free. 💉")
    outs = [Out(u.id, tip + f"\n\nTip: send a photo of {a.name} at feeding time and I'll keep their health diary.")]
    if not u.is_volunteer:
        outs.append(Out(u.id, "Would you also like to help other animals near you when they're hurt? 🦸",
                        buttons=[("vol:yes", "Yes, I'll help"), ("vol:no", "Not now")]))
    return outs


def checkin_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"] = photo_id
    if b.startswith("ci:yes:"):
        return checkin_log(s, u, int(b.split(":")[2]), d)
    if b == "ci:no":
        u.state = {}
        return [Out(u.id, "Okay! If it's a new friend, you can register them. 💛",
                    buttons=[("a:register", "Register new"), ("menu", "Menu")])]
    if not d.get("photo"):
        u.state = {"flow": "checkin", "d": d}
        return [Out(u.id, "Send today's photo. 📸")]
    lat, lon = d.get("lat") or u.lat, d.get("lon") or u.lon
    mine = my_animal_rows(s, u.id)
    near = [a for dist, a in nearby(s, Animal, lat, lon, 0.5)] if lat else []
    cands = list({a.id: a for a in mine + near if a.photo_id and a.status == "active"}.values())[:6]
    u.state = {"flow": "checkin", "d": d}
    if not cands:
        u.state = {"flow": "register", "d": {"photo": d["photo"]}}
        return [Out(u.id, "You haven't registered any animals yet, so let's register this one! 💛")] + \
            register_step(s, u, "", None, "")
    img = photos.data_url(s, d["photo"])
    mres = ai.match(s, u.id, photos.raw(s, d["photo"]), [(a.id, photos.raw(s, a.photo_id), f"{a.name}: {describe_line(a.profile)}")
                                    for a in cands], "daily check-in by a caretaker")
    if mres["key"] and mres["confidence"] >= 0.5:
        a = s.get(Animal, mres["key"])
        return [Out(u.id, f"Is this *{a.name}*? 🐾 (I compared {len(cands)} animals you know.)",
                    buttons=[(f"ci:yes:{a.id}", f"Yes, {a.name}"[:20]), ("ci:no", "Someone else")])]
    opts = [(f"ci:yes:{a.id}", a.name[:20]) for a in mine[:2]] + [("ci:no", "Someone else")]
    return [Out(u.id, "I'm not sure who this is. 🤔 Which one is it?", buttons=opts)]


def checkin_log(s, u: User, animal_id: int, d: dict) -> list[Out]:
    a = s.get(Animal, animal_id)
    u.state = {}
    if not a or not d.get("photo"):
        return [menu(u)]
    if u.id not in (a.guardians or []):
        a.guardians = list(a.guardians or []) + [u.id]
    last = s.scalars(select(CheckIn).where(CheckIn.animal_id == a.id, CheckIn.photo_id.is_not(None))
                     .order_by(CheckIn.created.desc())).first()
    old = photos.data_url(s, last.photo_id if last else a.photo_id)
    h = ai.compare_health(s, u.id, old, photos.data_url(s, d["photo"]))
    s.add(CheckIn(animal_id=a.id, user_id=u.id, photo_id=d["photo"], note=h["note"], concern=h["changed"]))
    lat, lon = d.get("lat") or a.lat, d.get("lon") or a.lon
    if lat:
        add_sighting(s, u.id, d["photo"], a.profile, lat, lon, "checkin", check=False)  # a known, cared-for animal
    days = s.scalar(select(func.count()).select_from(CheckIn).where(CheckIn.animal_id == a.id))
    outs = []
    if h["changed"]:
        msg = f"*Something's new on {a.name}* 💛\n{h['note']}"
        if h["see_vet"]:
            msg += "\nIf it gets worse or doesn't improve in a day, please show a vet."
        msg += "\n(This is not a diagnosis, just a change I noticed.)"
        outs.append(Out(u.id, msg))
        outs.append(Out(u.id, first_aid_text("sick", include_bite_rule=False)))
    else:
        outs.append(Out(u.id, f"Logged! ✅ {a.name} looks the same as last time. "
                              f"{'First check-in in the diary!' if days == 1 else f'That makes {days} check-ins so far.'} 🐾"))
    v = a.rabies_vaccinated_on
    due = v in (None, "unknown") or (v and v[:4].isdigit() and (now().date() - __import__('datetime').date.fromisoformat(v)).days > 330)
    if due:
        outs.append(Out(u.id, f"💉 Reminder: {a.name}'s anti-rabies vaccine may be due. Reply *vaccinated* once it's done."))
        u.state = {"flow": "vax_reply", "d": {"animal": a.id}}
    return outs


def my_animals(s, u: User) -> Out:
    rows = my_animal_rows(s, u.id)
    if not rows:
        return Out(u.id, "You haven't registered any animals yet. 💛", buttons=[("a:register", "Register one")])
    lines = []
    for a in rows:
        n = s.scalar(select(func.count()).select_from(CheckIn).where(CheckIn.animal_id == a.id))
        vax = "💉 vaccinated" if (a.rabies_vaccinated_on or "")[:2].isdigit() else "💉 vaccine due?"
        lines.append(f"• *{a.name}* ({a.species}) · {n} check-in{'' if n == 1 else 's'} · {vax}")
    return Out(u.id, "Your animals 🐾\n" + "\n".join(lines), buttons=[("a:checkin", "Daily check-in"),
                                                                      ("a:register", "Register another")])


# ------------------------------------------------------------------ LOST & FOUND

def lost_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"], d["desc"] = photo_id, None
    if not d.get("photo"):
        u.state = {"flow": "lost", "d": d}
        return [Out(u.id, "Please send a photo of your animal. An old photo is fine. 📸")]
    if not d.get("desc"):
        d["desc"] = ai.describe(s, u.id, photos.data_url(s, d["photo"]), text)
        u.state = {"flow": "lost", "d": d}
        return [Out(u.id, "What's their name?")]
    if not d.get("name"):
        if not text:
            return [Out(u.id, "What's their name?")]
        d["name"] = text.strip()[:40].title()
        u.state = {"flow": "lost", "d": d}
    if d.get("lat") is None:
        u.state = {"flow": "lost", "d": d}
        return [Out(u.id, f"Where was {d['name']} last seen? Share that location 📍 (it can be approximate).",
                    ask_location=True)]
    if not d.get("when"):
        if not text or text.title() == d["name"]:
            u.state = {"flow": "lost", "d": d}
            return [Out(u.id, "When did you last see them? (e.g. *yesterday evening*)")]
        d["when"] = text[:100]
    if b not in ("lost:public", "lost:private"):
        u.state = {"flow": "lost", "d": d}
        p = d["desc"]
        return [Out(u.id, f"*Let's find {d['name']} together* 💛\n{p['species'].title()} · {describe_line(p)}\n"
                          f"Last seen: {d['when']}\n\nMake a public poster that anyone can share and recognise? "
                          "Your number and the exact spot are never shown.",
                    buttons=[("lost:public", "Yes, make poster"), ("lost:private", "Keep private")])]
    p = d["desc"]
    post = LostPost(code="LF-" + str(secrets.randbelow(9000) + 1000), owner_id=u.id, name=d["name"],
                    species=p["species"], profile=p, photo_id=d["photo"], lat=d["lat"], lon=d["lon"],
                    last_seen_text=d["when"], public=(b == "lost:public"))
    s.add(post)
    s.flush()
    u.state = {}
    step(s, u.id, "Created a lost post", f"{post.code} for {post.name} ({'public' if post.public else 'private'})")
    outs = [Out(u.id, f"📣 Poster {post.code} is live! Share it in your local groups:" if post.public else
                f"Saved privately as {post.code}. I'll still check every sighting people send nearby.",
                link=f"{config.PUBLIC_BASE_URL}/lost/{post.code}" if post.public else None)]
    outs += search_sightings_for(s, post)
    return outs


def search_sightings_for(s, post: LostPost) -> list[Out]:
    since = now() - timedelta(days=config.LOST_SEARCH_DAYS)
    rows = [x for dist, x in nearby(s, Sighting, post.lat, post.lon, config.LOST_SEARCH_RADIUS_KM, species=post.species)
            if x.created >= since and x.user_id != post.owner_id and x.id not in (post.rejected_sightings or [])]
    step(s, post.owner_id, "Searched past sightings", f"{len(rows)} {post.species} photo(s) within "
                                                       f"{config.LOST_SEARCH_RADIUS_KM:g} km in the last {config.LOST_SEARCH_DAYS} days")
    if not rows:
        return [Out(post.owner_id, "No matching sightings yet. I'll check every new photo people send near there "
                                   "and message you the moment one looks like them. 🔎")]
    rows = rows[:18]
    best = None
    owner_img = photos.raw(s, post.photo_id)
    for i in range(0, len(rows), 9):  # one contact sheet holds up to 9 photos
        chunk = rows[i:i + 9]
        mres = ai.match(s, post.owner_id, owner_img,
                        [(x.id, photos.raw(s, x.photo_id), f"seen {ist(x.created)}") for x in chunk],
                        f"lost & found: is the lost {post.species} '{post.name}' in any of these sightings?",
                        step="Searched sightings for a match")
        if mres["key"] and mres["confidence"] >= 0.6 and (not best or mres["confidence"] > best[1]["confidence"]):
            best = (s.get(Sighting, mres["key"]), mres)
    if not best:
        return [Out(post.owner_id, f"I checked {len(rows)} recent sightings. None look like {post.name} yet. "
                                   "I'll keep watching every new photo. 🔎")]
    return [match_message(s, post, best[0], best[1], checked=len(rows))]


def match_message(s, post: LostPost, sight: Sighting, mres: dict, checked: int | None = None) -> Out:
    dist = km(post.lat, post.lon, sight.lat, sight.lon)
    extra = f"\n(I checked {checked} photos sent nearby.)" if checked else ""
    return Out(post.owner_id, f"*Maybe {post.name}!* 🔎💛\nSeen {ist(sight.created)} about {dist:.1f} km "
                              f"from where they were lost.{extra}\nPlease look at the photo carefully. The exact spot is shared once you confirm.",
               image_url=photo_url(sight.photo_id),
               buttons=[(f"lm:yes:{post.id}:{sight.id}", f"Yes, it's {post.name}"[:20]),
                        (f"lm:no:{post.id}:{sight.id}", "Not them")])


def lost_match_button(s, u: User, b: str) -> list[Out]:
    _, ans, pid, sid = b.split(":")
    post, sight = s.get(LostPost, int(pid)), s.get(Sighting, int(sid))
    if not post or not sight or post.owner_id != u.id:
        return [Out(u.id, "That match isn't available any more.")]
    if ans == "no":
        post.rejected_sightings = list(post.rejected_sightings or []) + [sight.id]
        return [Out(u.id, "Thanks for checking. I'll keep looking. 💛")]
    post.status, post.found_at = "found", now()
    step(s, u.id, "Owner confirmed a match", f"{post.code}: found after {(post.found_at - post.created).total_seconds() / 3600:.1f} h")
    spotter = s.get(User, sight.user_id)
    if spotter:
        spotter.trust = (spotter.trust or 0) + 1
    return [Out(u.id, f"🎉 Wonderful! Here's exactly where {post.name} was seen. Please go with someone, and bring "
                      "the ear tag details or old photos as proof.", link=maps_link(sight.lat, sight.lon)),
            Out(sight.user_id, f"🎉 The photo you sent helped! The owner confirmed it's {post.name}. Thank you! 💛")]


def seen_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"] = photo_id
    if not d.get("photo"):
        u.state = {"flow": "seen", "d": d}
        return [Out(u.id, "Please send a photo of the animal you saw. 📸")]
    if d.get("lat") is None:
        u.state = {"flow": "seen", "d": d}
        return [need_location(u)]
    desc = d.get("desc") or ai.describe(s, u.id, photos.data_url(s, d["photo"]), text)
    u.state = {}
    sight = add_sighting(s, u.id, d["photo"], desc, d["lat"], d["lon"], "seen", lost_code=d.get("code"), check=False)
    outs = check_lost_posts(s, sight, only_code=d.get("code"))
    if outs:
        return [Out(u.id, "Thank you! 💛 This may be a lost animal, so I've told the owner (your number stays private).")] + outs
    return [Out(u.id, "Thank you! 💛 I saved your sighting. If someone reports this animal lost, I'll check your "
                      "photo straight away.")]


def add_sighting(s, user_id, photo_id, desc, lat, lon, source, lost_code=None, check=True) -> Sighting:
    sight = Sighting(user_id=user_id, photo_id=photo_id, species=(desc or {}).get("species"), profile=desc or {},
                     lat=lat, lon=lon, source=source, lost_code=lost_code)
    s.add(sight)
    s.flush()
    if check:
        s.info.setdefault("pending_lost_checks", []).append(sight.id)
    return sight


def check_lost_posts(s, sight: Sighting, only_code: str | None = None) -> list[Out]:
    """Does this new photo show any animal someone has reported lost nearby?"""
    q = [p for dist, p in nearby(s, LostPost, sight.lat, sight.lon, config.LOST_SEARCH_RADIUS_KM)
         if p.status == "lost" and p.owner_id != sight.user_id and (p.species == sight.species or only_code)]
    if only_code:
        q = [p for p in q if p.code == only_code] or [p for p in s.scalars(select(LostPost).where(LostPost.code == only_code))]
    q = [p for p in q if sight.id not in (p.rejected_sightings or [])][:6]
    if not q:
        return []
    mres = ai.match(s, sight.user_id, photos.raw(s, sight.photo_id), [(p.id, photos.raw(s, p.photo_id), f"lost {p.species} '{p.name}'") for p in q],
                    "a new sighting: is it one of these lost animals?", step="Checked lost-animal posters")
    if mres["key"] and mres["confidence"] >= 0.6:
        post = s.get(LostPost, mres["key"])
        step(s, sight.user_id, "Possible lost-animal match", f"{post.code} {post.name} ({mres['confidence']:.0%})")
        return [match_message(s, post, sight, mres)]
    return []


def run_pending_lost_checks(s) -> list[Out]:
    ids = s.info.pop("pending_lost_checks", [])
    outs = []
    for sid in ids:
        sight = s.get(Sighting, sid)
        if sight:
            outs += check_lost_posts(s, sight)
    return outs


# ------------------------------------------------------------------ small flows

def handle_misc(s, u: User, inc: Incoming) -> list[Out] | None:
    st = (u.state or {}).get("flow")
    b, text = inc.button or "", (inc.text or "").strip().lower()
    if b.startswith("vax:") and st == "vax":
        return vax_answer(s, u, b)
    if st == "vax_reply" and text in ("vaccinated", "done", "yes", "haan", "ho gaya"):
        a = s.get(Animal, u.state["d"]["animal"])
        u.state = {}
        if a:
            a.rabies_vaccinated_on = now().date().isoformat()
            return [Out(u.id, f"💉 Noted! {a.name} is vaccinated. Thank you for keeping everyone safe. 💛")]
    if st == "volunteer_loc" and inc.lat is not None:
        u.lat, u.lon, u.state = inc.lat, inc.lon, {}
        return volunteer_join(s, u)
    if st == "volunteer_loc" and text and not b and text not in ("menu", "cancel", "stop"):
        from . import geo
        hit = geo.geocode(inc.text or "", None)
        if not hit:
            return [Out(u.id, "I couldn't find that area on the map. Please add the city (e.g. *Lalpur, Ranchi*) or "
                              "share your location 📍.", ask_location=True)]
        u.lat, u.lon, u.state = hit["lat"], hit["lon"], {}
        return [Out(u.id, f"📍 Saved your area as *{hit['label']}*.")] + volunteer_join(s, u)
    if text == "pause":
        u.state = {**(u.state or {}), "paused": True}
        return [Out(u.id, "Alerts paused. Type *resume* to turn them back on.")]
    if text == "resume":
        st2 = dict(u.state or {})
        st2.pop("paused", None)
        u.state = st2
        return [Out(u.id, "Alerts are back on. Thank you! 🦸")]
    return None


_orig_handle = handle


def handle(s: Session, user_id: str, inc: Incoming) -> list[Out]:  # noqa: F811
    """Entry point: small flows first, then the main router, then any lost-animal checks the new photos triggered."""
    u = get_or_create_user(s, user_id)
    if u.lang:
        if inc.lat is not None and (u.state or {}).get("flow") == "volunteer_loc":
            return handle_misc(s, u, inc) or []
        r = handle_misc(s, u, inc)
        if r is not None:
            return r
    outs = _orig_handle(s, user_id, inc)
    outs += run_pending_lost_checks(s)
    flag_modified(u, "state")  # JSON column: make sure every state change is saved
    return outs


# ------------------------------------------------------------------ translation at the edge

def translate_texts(s: Session, texts: list[str], lang: str) -> dict[str, str]:
    """Translate many strings into one language, using the shared cache; one AI call per 12 new strings."""
    import hashlib
    out = {t: t for t in texts}
    if not lang or lang == "en" or lang not in ai.LANGS:
        return out
    misses = []
    for t in dict.fromkeys(x for x in texts if x):
        hit = s.get(TranslationCache, hashlib.sha256(f"{lang}|{t}".encode()).hexdigest()[:64])
        if hit:
            out[t] = hit.text
        else:
            misses.append(t)
    for i in range(0, len(misses), 12):
        chunk = misses[i:i + 12]
        for t, tr in zip(chunk, ai.translate_batch(chunk, lang)):
            out[t] = tr
            if tr != t:
                s.merge(TranslationCache(key=hashlib.sha256(f"{lang}|{t}".encode()).hexdigest()[:64], text=tr))
    return out


def localise(s: Session, outs: list[Out]) -> list[Out]:
    """Translate every message into its recipient's language: cached, and one AI call per language."""
    import hashlib

    def key(lang, t):
        return hashlib.sha256(f"{lang}|{t}".encode()).hexdigest()[:64]

    want: dict[str, list[str]] = {}
    langs = {}
    for o in outs:
        if o.raw:
            continue
        r = s.get(User, o.to)
        lang = (r.lang if r else None) or "en"
        langs[id(o)] = lang
        if lang != "en":
            want.setdefault(lang, [])
            for t in [o.text] + [t for _, t in o.buttons] + [t for _, t in o.options]:
                if t and t not in want[lang]:
                    want[lang].append(t)
    done: dict[tuple[str, str], str] = {}
    for lang, texts in want.items():
        misses = []
        for t in texts:
            hit = s.get(TranslationCache, key(lang, t))
            if hit:
                done[(lang, t)] = hit.text
            else:
                misses.append(t)
        for i in range(0, len(misses), 12):
            chunk = misses[i:i + 12]
            for t, tr in zip(chunk, ai.translate_batch(chunk, lang)):
                done[(lang, t)] = tr
                if tr != t:
                    s.merge(TranslationCache(key=key(lang, t), text=tr))
    for o in outs:
        lang = langs.get(id(o), "en")
        if o.raw or lang == "en":
            continue
        o.text = done.get((lang, o.text), o.text)
        o.buttons = [(i, done.get((lang, t), t)[:20]) for i, t in o.buttons]
        o.options = [(i, done.get((lang, t), t)[:24]) for i, t in o.options]
    return outs
