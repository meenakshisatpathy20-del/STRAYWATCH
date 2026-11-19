"""Vet Care: talk to a verified vet online, for a pet or any animal (stray, cow, a rescue in progress).

Flow: who is it for → what animal → photo + a few words → AI pre-check (summary for the vet, a reviewed care card,
an urgency; code red-flag words can only make it MORE urgent) → emergencies go to a clinic / 1962 first → pick an
online verified vet → live chat (each side in their own language, photos, video link) → the vet writes a note →
follow-up after 2 days.

Safety: only verified, registered vets advise. The AI organises and routes; it never diagnoses, names a medicine or
gives a dose. Where India's rules on online prescribing are unclear, the vet decides and anything urgent goes in person.
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from . import ai, config, photos
from .db import Animal, CheckIn, Consult, ConsultMessage, User, VetProfile, now

DATA = Path(__file__).resolve().parent.parent / "data"
CARDS = json.loads((DATA / "vet_cards.json").read_text(encoding="utf-8"))
FOOD = json.loads((DATA / "food_guide.json").read_text(encoding="utf-8"))
URGENCY_RANK = {"watch": 0, "this_week": 1, "today": 2, "emergency": 3}
REPLY_WAIT_MIN = 5            # minutes a vet gets to accept before the next vet is asked
FOLLOW_UP_DAYS = 2
SPECIES_BUTTONS = [("v:sp:dog", "🐕 Dog"), ("v:sp:cat", "🐈 Cat"), ("v:sp:cow", "🐄 Cow / buffalo"),
                   ("v:sp:goat", "🐐 Goat"), ("v:sp:other", "Other")]


def _out(*a, **k):
    from .engine import Out
    return Out(*a, **k)


def _step(s, uid, title, detail):
    from .engine import step
    step(s, uid, title, detail)


# ------------------------------------------------------------------ helpers

def urgency_text(urgency: str) -> str:
    return "⏱ " + CARDS["urgency_words"].get(urgency, CARDS["urgency_words"]["today"])


def card_text(card_id: str, urgency: str = "") -> str:
    """The reviewed card. The urgency is a separate message, so each card is one fixed, pre-translatable text."""
    c = CARDS["cards"].get(card_id) or CARDS["cards"]["general"]
    lines = [f"*{c['title']}*", "", "*Do now:*"]
    lines += [f"• {x}" for x in c["do_now"]] + ["", "*Please don't:*"] + [f"• {x}" for x in c["dont"]]
    lines += ["", c["see_vet"], "Sources: " + ", ".join(c["sources"]) + ". Safety guidance, not a diagnosis."]
    return "\n".join(lines)


def guide_text(sp: str) -> str:
    g = FOOD["guides"].get(sp) or FOOD["guides"]["dog"]
    return (f"*{g['title']}*\n" + "\n".join("• " + x for x in g["lines"]) +
            "\n\nAsk me about any food: type its name, e.g. *roti*, *milk* or *chocolate*.")


def food_text(f: dict, verdict: str) -> str:
    word = {"safe": "✅ Okay", "limit": "⚠️ Only a little", "avoid": "❌ Don't feed"}[verdict]
    return f"{word}: {f['why']} ({f['src']})\n\nAsk about another food, or tap Menu."


def red_flag_urgency(text: str) -> str | None:
    t = (text or "").lower()
    for level in ("emergency", "today"):
        if any(w in t for w in CARDS["red_flags"][level]):
            return level
    return None


def vet_label(v: VetProfile, who: str) -> str:
    fee = "free" if (v.fee == 0 or (who != "pet" and v.free_for_strays)) else f"₹{v.fee}"
    return f"{v.name} · {fee}"


def online_vets(s, c: Consult) -> list[VetProfile]:
    vets = [v for v in s.scalars(select(VetProfile).where(VetProfile.verified.is_(True), VetProfile.online.is_(True),
                                                          VetProfile.user_id.is_not(None)))
            if v.id not in (c.asked or []) and (not v.species or c.species in v.species or "any" in v.species)]
    me = s.get(User, c.user_id)
    lang = (me.lang if me else None) or "en"
    # Speaks my language first, then free for strays (for street animals), then cheapest.
    return sorted(vets, key=lambda v: (lang not in (v.langs or []), not (c.who != "pet" and v.free_for_strays), v.fee))


def handover(s, c: Consult) -> str:
    """The one-page case summary the vet sees before saying yes."""
    sm = c.summary or {}
    who = {"pet": "Pet owner", "stray": "Caretaker of a street animal", "case": "Rescuer at a StrayWatch rescue"}[c.who]
    a = s.get(Animal, c.animal_id) if c.animal_id else None
    lines = [f"🩺 *Consult request C-{c.id}* · {CARDS['urgency_words'].get(c.urgency or 'today')}",
             f"{who}{' · ' + a.name if a else ''} · {sm.get('species', c.species or 'animal')} · age: {sm.get('age_guess', '?')}",
             f"Problem: {sm.get('problem', '')}",
             f"Since: {sm.get('since', 'not said')} · Eating/drinking: {sm.get('eating_drinking', 'not said')}",
             f"Summary: {sm.get('summary_for_vet', '')}"]
    if sm.get("symptoms"):
        lines.append("Symptoms: " + ", ".join(sm["symptoms"]))
    if a and a.rabies_vaccinated_on:
        lines.append(f"Rabies vaccine: {a.rabies_vaccinated_on}")
    if c.text:
        lines.append(f"Owner's words: “{c.text[:200]}”")
    return "\n".join(lines)


def active_consult(s, uid: str, prefer: int | None = None) -> Consult | None:
    """The live consult this person is in, as owner or as vet. A vet may have several open: use the one their
    chat is pointing at (`prefer`, from their state), else the newest."""
    if prefer:
        c = s.get(Consult, prefer)
        if c and c.status == "active":
            v = s.get(VetProfile, c.vet_id) if c.vet_id else None
            if c.user_id == uid or (v and v.user_id == uid):
                return c
    c = s.scalars(select(Consult).where(Consult.user_id == uid, Consult.status == "active")
                  .order_by(Consult.id.desc())).first()
    if c:
        return c
    vp = s.scalars(select(VetProfile).where(VetProfile.user_id == uid)).first()
    if vp:
        return s.scalars(select(Consult).where(Consult.vet_id == vp.id, Consult.status == "active")
                         .order_by(Consult.id.desc())).first()
    return None


# ------------------------------------------------------------------ the consult flow

def start(s, u: User, who: str = "", animal_id: int | None = None) -> list:
    if who:
        return _pick_who(s, u, who, animal_id)
    from .engine import my_animal_rows
    mine = my_animal_rows(s, u.id)[:2]
    opts = [("v:who:pet", "🏠 My pet"), ("v:who:stray", "🐾 A street animal")] + \
           [(f"v:who:a:{a.id}", f"💛 {a.name}") for a in mine]
    u.state = {"flow": "vet", "d": {}}
    return [_out(u.id, "🩺 *Talk to a vet*\nI'll ask a few quick questions so the vet can help faster. "
                       "Who is this for?", options=opts)]


def _pick_who(s, u: User, who: str, animal_id: int | None = None) -> list:
    d = {"who": "stray" if who.startswith("a") else who}
    if animal_id:
        a = s.get(Animal, animal_id)
        if a:
            d.update(animal_id=a.id, species=a.species)
    u.state = {"flow": "vet", "d": d}
    if d.get("species"):
        return [_ask_problem(u)]
    return [_out(u.id, "Which animal is it?", options=SPECIES_BUTTONS)]


def _ask_problem(u: User):
    return _out(u.id, "Tell me what's wrong: send a *photo* and a few words.\n"
                      "For example: what you see, since when, and is it eating and drinking?")


def flow_step(s, u: User, text: str, photo_id: str | None, b: str) -> list:
    d = dict((u.state or {}).get("d", {}))
    if b.startswith("v:who:"):
        rest = b[6:]
        return _pick_who(s, u, "a" if rest.startswith("a:") else rest, int(rest[2:]) if rest.startswith("a:") else None)
    if b.startswith("v:sp:"):
        d["species"] = b[5:]
        u.state = {"flow": "vet", "d": d}
        return [_ask_problem(u)]
    if "who" not in d:
        return start(s, u)
    if "species" not in d:
        return [_out(u.id, "Which animal is it?", options=SPECIES_BUTTONS)]
    if photo_id:
        d["photo"] = photo_id
    if text:
        d["text"] = (d.get("text", "") + " " + text).strip()
    u.state = {"flow": "vet", "d": d}
    if not d.get("text"):
        return [_out(u.id, "Got the photo. 📸 Now in a few words: what's wrong, since when, and is it eating?")]
    return intake(s, u, d)


def intake(s, u: User, d: dict) -> list:
    img = photos.data_url(s, d.get("photo"))
    sm = ai.vet_intake(s, u.id, img, d.get("text", ""), d.get("species", ""))
    flag = red_flag_urgency(d.get("text", ""))
    urgency = sm["urgency"]
    if flag and URGENCY_RANK[flag] > URGENCY_RANK[urgency]:
        _step(s, u.id, "Raised urgency", f"Words in the message mean at least '{flag}' (code rule).")
        urgency = flag
    c = Consult(user_id=u.id, who=d.get("who", "pet"), species=sm["species"] if d.get("species") in (None, "other") else d["species"],
                animal_id=d.get("animal_id"), case_id=d.get("case_id"), photo_id=d.get("photo"), text=d.get("text"),
                summary=sm, urgency=urgency, card=sm["card"], status="intake")
    s.add(c)
    s.flush()
    u.state = {"flow": "vet_ready", "d": {"consult": c.id}}
    _step(s, u.id, "Vet pre-check ready", f"C-{c.id}: {sm['problem']} → card '{sm['card']}', {urgency}")
    outs = [_out(u.id, urgency_text(urgency)), _out(u.id, card_text(sm["card"]))]
    if urgency == "emergency":
        outs.append(_out(u.id, "🚨 *This sounds like an emergency.* Please take the animal to the nearest vet clinic now, "
                               "or call the government animal ambulance on *1962*. I can also connect you to a vet online "
                               "while you go.", link=clinic_link(u),
                         buttons=[(f"v:connect:{c.id}", "Talk to a vet now"), ("v:done", "I'm going now")]))
    else:
        outs.append(_out(u.id, "Would you like a verified vet to look at this with you now?",
                         buttons=[(f"v:connect:{c.id}", "🩺 Talk to a vet"), ("v:done", "No, that helps")]))
    return outs


def clinic_link(u: User) -> str:
    if u.lat:
        return f"https://www.google.com/maps/search/veterinary+clinic/@{u.lat:.4f},{u.lon:.4f},14z"
    return "https://www.google.com/maps/search/veterinary+clinic+near+me"


def connect(s, u: User, cid: int) -> list:
    c = s.get(Consult, cid)
    if not c or c.user_id != u.id or c.status in ("active", "closed", "cancelled"):
        return [_out(u.id, "That consult isn't available any more.")]
    vets = online_vets(s, c)
    u.state = {}
    if not vets:
        c.status = "waiting"
        _step(s, u.id, "No vet online", f"C-{c.id} is waiting; the first vet to come online will see it.")
        return [_out(u.id, "No vet is online right now. 🙏 I've saved your request; the first vet who comes online will "
                           "see it, and I'll message you. If it gets worse, go to a clinic or call 1962.",
                     link=clinic_link(u), buttons=[(f"v:cancel:{c.id}", "Cancel request")])]
    if len(vets) == 1:
        return ask_vet(s, c, vets[0])
    return [_out(u.id, "These verified vets are online now. Who would you like to talk to?",
                 options=[(f"v:pick:{c.id}:{v.id}", vet_label(v, c.who)) for v in vets[:3]])]


def ask_vet(s, c: Consult, v: VetProfile) -> list:
    c.vet_id, c.status, c.requested_at = v.id, "requested", now()
    c.asked = list(c.asked or []) + [v.id]
    _step(s, c.user_id, "Asked a vet", f"C-{c.id} → {v.name}. Waiting {REPLY_WAIT_MIN} minutes.")
    return [_out(c.user_id, f"I've asked *{v.name}*. 🩺 Vets usually reply in a few minutes; if they can't, I'll ask "
                            "another vet by myself.", buttons=[(f"v:cancel:{c.id}", "Cancel request")]),
            _out(v.user_id, handover(s, c), image_url=_photo_url(c.photo_id),
                 buttons=[(f"v:accept:{c.id}", "Accept"), (f"v:decline:{c.id}", "Can't now")])]


def _photo_url(pid):
    from .engine import photo_url
    return photo_url(pid)


def button(s, u: User, b: str) -> list:
    """All v:… buttons that aren't part of the intake questions."""
    parts = b.split(":")
    act = parts[1]
    if act == "start":
        return start(s, u)
    if act == "done":
        u.state = {}
        return [_out(u.id, "Take care. 💛 If anything changes, tap *Talk to a vet* from the menu.")]
    if act == "connect":
        return connect(s, u, int(parts[2]))
    if act == "pick":
        c, v = s.get(Consult, int(parts[2])), s.get(VetProfile, int(parts[3]))
        if not c or not v or c.user_id != u.id or c.status not in ("intake", "waiting"):
            return [_out(u.id, "That consult isn't available any more.")]
        if not (v.verified and v.online):
            return [_out(u.id, f"{v.name} just went offline. Let me find another vet.")] + connect(s, u, c.id)
        return ask_vet(s, c, v)
    if act == "cancel":
        c = s.get(Consult, int(parts[2]))
        if c and c.user_id == u.id and c.status in ("intake", "waiting", "requested"):
            was = c.status
            c.status = "cancelled"
            u.state = {}
            out = [_out(u.id, "Okay, I've cancelled the request. 💛")]
            v = s.get(VetProfile, c.vet_id) if c.vet_id else None
            if v and v.user_id and was == "requested":
                out.append(_out(v.user_id, f"Consult C-{c.id} was cancelled by the owner."))
            return out
        return [_out(u.id, "Okay. 💛")]
    if act in ("accept", "decline"):
        return vet_answer(s, u, int(parts[2]), act == "accept")
    if act == "video":
        return video(s, u, int(parts[2]))
    if act == "note":
        c = s.get(Consult, int(parts[2]))
        if not c or c.status != "active" or not _is_vet_of(s, u, c):
            return [_out(u.id, "That consult isn't active.")]
        u.state = {"flow": "vet_note", "d": {"consult": c.id}}
        return [_out(u.id, "Please type your advice for the owner: what to do, any medicine you prescribe and how to "
                           "give it, and when to come back. It is saved as your visit note.")]
    if act == "end":
        return end_by_owner(s, u, int(parts[2]))
    if act == "fu":
        return follow_up_answer(s, u, int(parts[2]), parts[3])
    if act == "food":
        u.state = {"flow": "vet_food", "d": {}}
        return [_out(u.id, "🥣 *Food & care*: which animal?",
                     options=[("v:fd:dog", "🐕 Dog"), ("v:fd:puppy", "🐶 Puppy"), ("v:fd:cat", "🐈 Cat"), ("v:fd:cow", "🐄 Cow")])]
    if act == "fd":
        sp = parts[2]
        g = FOOD["guides"].get(sp)
        u.state = {"flow": "vet_food", "d": {"species": sp}}
        return [_out(u.id, guide_text(sp), buttons=[("v:start", "🩺 Talk to a vet"), ("menu", "Menu")])]
    if act == "case":  # a rescuer at an SOS asks a vet: the case details become the consult
        from .db import Case
        k = s.get(Case, int(parts[2]))
        if not k:
            return [_out(u.id, "That case was not found.")]
        t = k.triage or {}
        sm = {"species": t.get("species", "animal"), "age_guess": t.get("age_guess", "unknown"),
              "problem": t.get("injuries") or t.get("condition", "injured"), "symptoms": [], "since": "now (rescue)",
              "eating_drinking": "not said", "card": "wound_bleeding", "urgency": "today",
              "summary_for_vet": f"StrayWatch rescue SW-{k.id}: {t.get('summary', '')}", "ask": []}
        c = Consult(user_id=u.id, who="case", species=t.get("species"), case_id=k.id, animal_id=k.animal_id,
                    photo_id=k.photo_id, summary=sm, urgency="today", card="wound_bleeding", status="intake",
                    text="Rescuer at the scene")
        s.add(c)
        s.flush()
        return [_out(u.id, "🩺 Connecting you to a vet with this case's details…")] + connect(s, u, c.id)
    if act == "clinic":
        return [_out(u.id, "Vet clinics near you:", link=clinic_link(u))]
    return []


def _is_vet_of(s, u: User, c: Consult) -> bool:
    v = s.get(VetProfile, c.vet_id) if c.vet_id else None
    return bool(v and v.user_id == u.id)


def vet_answer(s, u: User, cid: int, yes: bool) -> list:
    c = s.get(Consult, cid)
    if not c or not _is_vet_of(s, u, c) or c.status != "requested":
        return [_out(u.id, "This request was already handled. Thank you! 🙏")]
    v = s.get(VetProfile, c.vet_id)
    if not yes:
        _step(s, c.user_id, "Vet can't take it", f"C-{c.id}: {v.name} declined; finding another vet.")
        return [_out(u.id, "No problem. I'll ask another vet. 🙏")] + next_vet(s, c)
    c.status, c.accepted_at = "active", now()
    mins = (c.accepted_at - (c.requested_at or c.created)).total_seconds() / 60
    _step(s, c.user_id, "Vet joined", f"C-{c.id}: {v.name} accepted after {mins:.1f} min.")
    owner = s.get(User, c.user_id)
    if owner:
        owner.state = {"flow": "consult", "d": {"consult": c.id}}
    u.state = {"flow": "consult", "d": {"consult": c.id}}
    reg = f" (Reg. {v.reg_no}, {v.council})" if v.reg_no else ""
    return [_out(c.user_id, f"✅ *{v.name}*{reg} has joined. 🩺\nType your messages here, and send photos. Everything you "
                            "write goes to the vet, in their language.",
                 buttons=[(f"v:video:{c.id}", "📹 Video call"), (f"v:end:{c.id}", "End chat")]),
            _out(u.id, f"You're connected for C-{c.id}. Messages you type now go to the owner, translated into their "
                       "language. When you're done, tap *End & write note*.",
                 buttons=[(f"v:video:{c.id}", "📹 Video call"), (f"v:note:{c.id}", "End & write note")])]


def next_vet(s, c: Consult) -> list:
    vets = online_vets(s, c)
    if vets:
        return ask_vet(s, c, vets[0])
    c.status, c.vet_id = "waiting", None
    owner = s.get(User, c.user_id)
    return [_out(c.user_id, "The vets online right now can't take it. 🙏 I've kept your request; the first vet who comes "
                            "online will see it. If it gets worse, go to a clinic or call 1962.",
                 link=clinic_link(owner) if owner else None, buttons=[(f"v:cancel:{c.id}", "Cancel request")])]


def relay(s, u: User, c: Consult, text: str, photo_id: str | None) -> list:
    """Live chat: pass a message to the other side. Each side reads it in their own language."""
    v = s.get(VetProfile, c.vet_id)
    from_vet = v and v.user_id == u.id
    to = c.user_id if from_vet else v.user_id
    s.add(ConsultMessage(consult_id=c.id, sender_id=u.id, text=text or None, photo_id=photo_id))
    label = f"🩺 {v.name}" if from_vet else "👤 Owner"
    return [_out(to, f"{label}: {text}" if text else f"{label} sent a photo.", image_url=_photo_url(photo_id))]


def video(s, u: User, cid: int) -> list:
    c = s.get(Consult, cid)
    if not c or c.status != "active":
        return [_out(u.id, "That consult isn't active.")]
    if not c.video_room:
        c.video_room = f"https://meet.jit.si/StrayWatch-C{c.id}-{secrets.token_urlsafe(9)}"
    v = s.get(VetProfile, c.vet_id)
    msg = "📹 Video call: open this link on both phones. No sign-up needed. You can keep chatting here too."
    return [_out(c.user_id, msg, link=c.video_room, buttons=[(f"v:end:{c.id}", "End chat")]),
            _out(v.user_id, msg, link=c.video_room, buttons=[(f"v:note:{c.id}", "End & write note")])]


def save_note(s, u: User, text: str) -> list:
    c = s.get(Consult, ((u.state or {}).get("d") or {}).get("consult", 0))
    u.state = {}
    if not c or c.status != "active" or not _is_vet_of(s, u, c):
        return [_out(u.id, "That consult isn't active.")]
    v = s.get(VetProfile, c.vet_id)
    c.note, c.status, c.closed_at = text.strip()[:3000], "closed", now()
    c.followup_at = now() + timedelta(days=FOLLOW_UP_DAYS)
    owner = s.get(User, c.user_id)
    if owner and (owner.state or {}).get("flow") == "consult":
        owner.state = {}
    if c.animal_id:
        s.add(CheckIn(animal_id=c.animal_id, user_id=c.user_id, note=f"Vet consult C-{c.id} with {v.name}: {c.note[:400]}"))
    _step(s, c.user_id, "Vet note saved", f"C-{c.id} closed by {v.name}; follow-up in {FOLLOW_UP_DAYS} days.")
    reg = f"{v.name}" + (f", Reg. {v.reg_no} ({v.council})" if v.reg_no else "")
    return [_out(c.user_id, f"📝 *Advice from {v.name}*\n{c.note}\n\n— {reg}. If it gets worse, go to a clinic or "
                            f"call 1962. I'll check in with you in {FOLLOW_UP_DAYS} days. 💛"),
            _out(u.id, f"Saved and sent to the owner. Thank you for helping, {v.name}! 🙏")]


def end_by_owner(s, u: User, cid: int) -> list:
    c = s.get(Consult, cid)
    if not c or c.user_id != u.id or c.status != "active":
        return [_out(u.id, "That chat has already ended.")]
    c.status, c.closed_at = "closed", now()
    u.state = {}
    v = s.get(VetProfile, c.vet_id)
    if v and v.user_id:
        vu = s.get(User, v.user_id)
        if vu and (vu.state or {}).get("flow") == "consult":
            vu.state = {}
    return [_out(u.id, "Chat ended. Take care. 💛"),
            _out(v.user_id, f"The owner ended consult C-{c.id}. Thank you! 🙏")]


def follow_up_answer(s, u: User, cid: int, how: str) -> list:
    c = s.get(Consult, cid)
    if not c or c.user_id != u.id:
        return []
    if how == "worse":
        c2 = Consult(user_id=u.id, who=c.who, species=c.species, animal_id=c.animal_id, summary={**(c.summary or {}),
                     "summary_for_vet": "FOLLOW-UP: worse since the last consult. Last advice: " + (c.note or "")[:300]},
                     urgency="today", card=c.card, status="intake", text="Worse after the last consult")
        s.add(c2)
        s.flush()
        return [_out(u.id, "I'm sorry. Let's get a vet to look again.")] + connect(s, u, c2.id)
    return [_out(u.id, "Glad to hear it. 💛 Keep following the vet's advice." if how == "better"
                 else "Okay. If it doesn't improve, tap below to talk to a vet again.",
                 buttons=[] if how == "better" else [(f"v:fu:{c.id}:worse", "Talk to a vet again")])]


def food_answer(s, u: User, text: str) -> list:
    sp = ((u.state or {}).get("d") or {}).get("species", "dog")
    t = (text or "").lower()
    for f in FOOD["foods"]:
        if any(n in t for n in f["names"]):
            v = f["verdict"].get(sp) or f["verdict"].get("dog" if sp == "puppy" else sp)
            if v not in ("safe", "limit", "avoid"):
                break
            return [_out(u.id, food_text(f, v), buttons=[("v:start", "🩺 Talk to a vet"), ("menu", "Menu")])]
    return [_out(u.id, "I don't have that food in my reviewed list, so I won't guess. 🙏 Please ask a vet.",
                 buttons=[("v:start", "🩺 Talk to a vet"), ("menu", "Menu")])]


# ------------------------------------------------------------------ vets: linking, online/offline, timers

def vet_command(s, u: User, text: str) -> list | None:
    """'vet VET-1234' links a chat to a vet profile; 'online' / 'offline' for linked vets."""
    t = (text or "").strip().lower()
    if t.startswith("vet vet-"):
        code = t.split()[1].upper()
        v = s.scalars(select(VetProfile).where(VetProfile.code == code)).first()
        if not v:
            return [_out(u.id, "I couldn't find that vet code. Please check the code you got after signing up.")]
        v.user_id, u.role = u.id, "vet"
        if not v.verified:
            return [_out(u.id, f"Thank you, {v.name}! 🙏 Your account is linked. We'll check your registration and "
                               "message you here when you're verified.")]
        return [_out(u.id, f"Welcome, {v.name}! 🩺 Type *online* to start receiving consults, *offline* to stop.")]
    if t in ("online", "offline"):
        v = s.scalars(select(VetProfile).where(VetProfile.user_id == u.id)).first()
        if not v:
            return None
        if not v.verified:
            return [_out(u.id, "Your registration is still being checked. We'll message you when you're verified.")]
        v.online = t == "online"
        outs = [_out(u.id, "You're *online*. 🩺 Consult requests will come here." if v.online else
                     "You're *offline*. No consult requests until you type *online*.")]
        if v.online:
            outs += assign_waiting(s)
        return outs
    return None


def assign_waiting(s) -> list:
    outs = []
    for c in s.scalars(select(Consult).where(Consult.status == "waiting").order_by(Consult.id)):
        vets = online_vets(s, c)
        if vets:
            outs += [_out(c.user_id, "A vet is online now. 🩺")] + ask_vet(s, c, vets[0])
    return outs


def due(s) -> list:
    """Called with the Watch agent's timer: unanswered requests move on; follow-ups go out."""
    outs = []
    cutoff = now() - timedelta(minutes=REPLY_WAIT_MIN)
    for c in s.scalars(select(Consult).where(Consult.status == "requested", Consult.requested_at < cutoff)):
        v = s.get(VetProfile, c.vet_id)
        _step(s, c.user_id, "Vet didn't reply", f"C-{c.id}: {v.name if v else 'vet'} didn't reply in {REPLY_WAIT_MIN} min.")
        if v and v.user_id:
            outs.append(_out(v.user_id, f"Consult C-{c.id} was passed to another vet. No problem. 🙏"))
        outs += next_vet(s, c)
    outs += assign_waiting(s)
    idle = now() - timedelta(hours=3)
    for c in s.scalars(select(Consult).where(Consult.status == "active", Consult.accepted_at < idle)):
        last = s.scalars(select(ConsultMessage.created).where(ConsultMessage.consult_id == c.id)
                         .order_by(ConsultMessage.id.desc())).first()
        if (last or c.accepted_at) < idle:
            c.status, c.closed_at = "closed", now()
            for uid in (c.user_id, getattr(s.get(VetProfile, c.vet_id), "user_id", None)):
                us = s.get(User, uid) if uid else None
                if us and (us.state or {}).get("d", {}).get("consult") == c.id:
                    us.state = {}
            outs.append(_out(c.user_id, f"Consult C-{c.id} closed after 3 hours without messages. Tap *Talk to a vet* "
                                        "any time to start again. 💛"))
    for c in s.scalars(select(Consult).where(Consult.status == "closed", Consult.followed_up.is_(False),
                                            Consult.followup_at.is_not(None), Consult.followup_at <= now())):
        c.followed_up = True
        a = s.get(Animal, c.animal_id) if c.animal_id else None
        outs.append(_out(c.user_id, f"💛 Checking in: how is {a.name if a else 'the animal'} today, after the vet's advice?",
                         buttons=[(f"v:fu:{c.id}:better", "Better"), (f"v:fu:{c.id}:same", "Same"),
                                  (f"v:fu:{c.id}:worse", "Worse")]))
    return outs


def new_code(s) -> str:
    while True:
        code = f"VET-{secrets.randbelow(9000) + 1000}"
        if not s.scalars(select(VetProfile).where(VetProfile.code == code)).first():
            return code
