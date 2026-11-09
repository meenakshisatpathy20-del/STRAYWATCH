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

from . import ai, config, photos
from .db import (Animal, Case, CheckIn, LostPost, Sighting, Trace, TranslationCache, User, get_or_create_user, km,
                 nearby, now)

log = logging.getLogger("straywatch.engine")
DATA = Path(__file__).resolve().parent.parent / "data"
FIRST_AID = json.loads((DATA / "first_aid.json").read_text(encoding="utf-8"))
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

    # 3. Flow steps.
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

    # 4. No flow: a photo on its own, or free text.
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
    return Out(u.id, "Please share the location 📍 (WhatsApp: tap 📎 → Location → Send your current location).",
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
                          "animals nearby.", ask_location=True)]
    return [Out(u.id, "You're a StrayWatch helper now! 🦸 I'll only alert you about animals within "
                      f"{config.HELPER_RADIUS_KM:g} km, at most {config.MAX_ALERTS_PER_HELPER_PER_DAY} times a day. "
                      "Type *pause* to stop alerts.")]


# ------------------------------------------------------------------ RESCUE SOS

def sos_step(s, u: User, text: str, photo_id: str | None, b: str) -> list[Out]:
    d = dict((u.state or {}).get("d", {}))
    if photo_id:
        d["photo"] = photo_id
        d.pop("desc", None)
    if text:
        d["text"] = (d.get("text", "") + " " + text).strip()
    u.state = {"flow": "sos", "d": d}
    if not d.get("photo") and not d.get("text"):
        return [Out(u.id, "Please send a photo of the animal, or describe it in words if it's dark.")]
    if d.get("lat") is None:
        if u.lat is not None and b == "sos:here":
            d["lat"], d["lon"] = u.lat, u.lon
        else:
            return [Out(u.id, "Got it. Now please share the location 📍 so help can find the animal.",
                        ask_location=True)]
    return create_case(s, u, d)


def create_case(s, u: User, d: dict) -> list[Out]:
    lat, lon = d["lat"], d["lon"]
    img = photos.data_url(s, d.get("photo"))
    desc = d.get("desc") or ai.describe(s, u.id, img, d.get("text", ""))
    desc = apply_reporter_words(s, u.id, desc, d.get("text", ""))
    u.state = {}
    if img and not desc["is_animal"] and desc["confidence"] >= 0.7:
        step(s, u.id, "Report not sent", "No animal visible in the photo, so no helper was disturbed.")
        u.trust = (u.trust or 0) - 1
        return [Out(u.id, "I couldn't see an animal in this photo, so I haven't alerted anyone. If it's dark, "
                          "please describe the animal in words and share the location again.")]

    # Same animal already reported nearby in the last 2 hours? Merge instead of sending a second rescue.
    recent = [c for dist, c in nearby(s, Case, lat, lon, 0.2)
              if c.status in ("alerting", "assigned") and c.created > now() - timedelta(hours=2)
              and (c.triage or {}).get("species") == desc["species"]]
    if recent:
        c = recent[0]
        c.merged_reports = (c.merged_reports or 1) + 1
        step(s, u.id, "Merged with an open case", f"SW-{c.id} already open ({c.merged_reports} reports), no duplicate rescue")
        if d.get("photo"):
            add_sighting(s, u.id, d["photo"], desc, lat, lon, "sos")
        msg = "Someone already reported this animal and help is " + (
            "on the way. 🙏" if c.status == "assigned" else "being alerted right now. 🙏")
        return [Out(u.id, f"*Case SW-{c.id}*\n{msg}\nThank you for looking out for them."),
                Out(u.id, first_aid_text(desc["first_aid_card"]))]

    case = Case(reporter_id=u.id, photo_id=d.get("photo"), triage=desc, lat=lat, lon=lon, status="alerting")
    s.add(case)
    s.flush()

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
    caretakers = [g for g in (animal.guardians if animal else []) if g != u.id]
    helpers = pick_helpers(s, lat, lon, exclude={u.id, *caretakers})
    case.queue = caretakers + helpers
    step(s, u.id, "Planned who to ask", f"{len(caretakers)} caretaker(s) first, then {len(helpers)} nearby helper(s). "
                                        f"Each gets {wait_words()} to reply.")
    if d.get("photo"):
        # A recognised registered animal is not someone's lost pet: don't send its photo to lost-poster matching.
        add_sighting(s, u.id, d["photo"], desc, lat, lon, "sos", check=animal is None)

    urg = {"high": "URGENT", "medium": "Needs help soon", "low": "Not urgent", "none": "Not urgent"}[desc["urgency"]]
    intro = f"*Case SW-{case.id} · {urg}*\n{desc['summary']}"
    if animal:
        intro += f"\n\nThis may be *{animal.name}*, a registered community animal. 💛 Their caretaker lives close by, so I'm telling them first."
    outs = [Out(u.id, intro), Out(u.id, first_aid_text(desc["first_aid_card"]))]
    outs += alert_next(s, case)
    return outs


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
    return d


def wait_words() -> str:
    sec = config.ESCALATE_AFTER_SECONDS
    return f"{sec // 60} minutes" if sec >= 120 else (f"{sec} seconds" if sec < 60 else "1 minute")


def pick_helpers(s, lat, lon, exclude: set) -> list[str]:
    since = now() - timedelta(days=1)
    out = []
    for dist, h in nearby(s, User, lat, lon, config.HELPER_RADIUS_KM, is_volunteer=True):
        if h.id in exclude or (h.state or {}).get("paused"):
            continue
        asked_today = sum(1 for c in s.scalars(select(Case).where(Case.created >= since)) if h.id in (c.asked or []))
        if asked_today >= config.MAX_ALERTS_PER_HELPER_PER_DAY:
            continue
        out.append(h.id)
    return out[:8]


def alert_next(s, case: Case) -> list[Out]:
    q = list(case.queue or [])
    while q:
        hid = q.pop(0)
        h = s.get(User, hid)
        if not h:
            continue
        case.queue, case.current_helper, case.alerted_at = q, hid, now()
        case.asked = list(case.asked or []) + [hid]
        is_caretaker = False
        name = "an animal"
        if case.animal_id:
            a = s.get(Animal, case.animal_id)
            if a and hid in (a.guardians or []):
                is_caretaker, name = True, a.name
        dist = km(h.lat, h.lon, case.lat, case.lon)
        where = f"{dist:.1f} km from you" if dist != float("inf") else "near you"
        head = (f"*This may be {name}. They need you!* 🚨 (please check the photo)" if is_caretaker
                else "*An animal near you needs help* 🚨")
        tri = case.triage or {}
        step(s, case.reporter_id, "Alerted a " + ("caretaker" if is_caretaker else "nearby helper"),
             f"SW-{case.id} → helper #{len(case.asked)} ({where}). Waiting {wait_words()}.")
        return [Out(hid, f"{head}\nCase SW-{case.id}: {tri.get('summary', '')}\nWhere: {where}\n"
                         f"If you can't reply in {wait_words()}, I'll ask the next helper.",
                    image_url=photo_url(case.photo_id),
                    buttons=[(f"case:go:{case.id}", "On my way"), (f"case:no:{case.id}", "Can't make it")])]
    case.queue, case.current_helper = [], None
    if case.status == "alerting":
        case.status = "unanswered"
        step(s, case.reporter_id, "No helper free", f"SW-{case.id}: everyone nearby was asked. Told the reporter who else to call.")
        return [Out(case.reporter_id,
                    f"I'm sorry, no StrayWatch helper near you is free right now (case SW-{case.id}). 🙏\n"
                    "Please call a local animal welfare group or your municipal corporation's animal helpline. "
                    "If a person is hurt, call 112.\nI'll keep this case open and alert new helpers who join nearby.")]
    return []


def escalate_due(s) -> list[Out]:
    """Called every ~20 s: anyone who didn't answer in time is skipped."""
    outs = []
    cutoff = now() - timedelta(seconds=config.ESCALATE_AFTER_SECONDS)
    for c in s.scalars(select(Case).where(Case.status == "alerting", Case.alerted_at < cutoff)):
        step(s, c.reporter_id, "No reply in time", f"SW-{c.id}: moving to the next helper.")
        nxt = alert_next(s, c)
        if c.status == "alerting":  # keep the person on the roadside informed, once per hop
            outs.append(Out(c.reporter_id, f"⏳ Still finding help for case SW-{c.id}. The first person couldn't reply, "
                                           f"so I've asked helper #{len(c.asked)} nearby. Please stay safe."))
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
        step(s, c.reporter_id, "Helper accepted", f"SW-{c.id} accepted after {mins:.1f} min")
        is_ct = bool(c.animal_id and u.id in (s.get(Animal, c.animal_id).guardians or []))
        who = "The caretaker" if is_ct else "A StrayWatch helper"
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
        step(s, c.reporter_id, "Helper can't go", f"SW-{c.id}: asking the next one")
        nxt = alert_next(s, c)
        upd = [Out(c.reporter_id, f"⏳ Still finding help for case SW-{c.id}: asking helper #{len(c.asked)} nearby.")] \
            if c.status == "alerting" else []
        return [Out(u.id, "No problem, I'll ask someone else. 💛")] + upd + nxt
    if action == "safe":
        c.status, c.closed_at = "closed", now()
        rep = s.get(User, c.reporter_id)
        if rep:
            rep.trust = (rep.trust or 0) + 1
        mins = (c.closed_at - c.created).total_seconds() / 60
        step(s, c.reporter_id, "Case closed", f"SW-{c.id} safe after {mins:.0f} min")
        if c.animal_id:
            s.add(CheckIn(animal_id=c.animal_id, user_id=u.id, note=f"Rescued (case SW-{c.id})", concern=True))
        return [Out(u.id, "You're a hero! 🦸💛 Case closed. If the animal needs a vet, please take it to the nearest clinic."),
                Out(c.reporter_id, f"🎉 Good news! The animal from case SW-{c.id} is safe now. Thank you for caring.")]
    if action == "more":
        c.queue = pick_helpers(s, c.lat, c.lon, exclude={c.reporter_id, *(c.asked or [])})
        step(s, c.reporter_id, "Extra help requested", f"SW-{c.id}: asking one more helper")
        nxt = alert_next(s, c) if c.queue else []
        c.status = "assigned"
        return [Out(u.id, "I'm asking one more helper nearby." if nxt else
                    "No other helper is free nearby. Please call a local animal welfare group or the municipal helpline.")] + nxt
    return []


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
                              f"from where they were lost.\nWhy: {mres['reason']}{extra}\nThe exact spot is shared once you confirm.",
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
