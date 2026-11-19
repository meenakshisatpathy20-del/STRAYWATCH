"""Vet Care: AI pre-check → care card → verified online vet → live chat → vet's note → follow-up."""
from datetime import timedelta

from app import ai, engine, vet
from app.db import Consult, ConsultMessage, User, VetProfile, now
from test_engine import photo, s, talk, texts  # noqa: F401


def intake(**kw):
    base = {"species": "dog", "age_guess": "puppy", "problem": "vomiting", "symptoms": ["vomiting"], "since": "2 days",
            "eating_drinking": "not eating", "card": "vomiting_diarrhoea", "urgency": "today",
            "summary_for_vet": "Puppy vomiting for 2 days.", "ask": [], "ai_ok": True}
    base.update(kw)
    return lambda *a, **k: dict(base)


def add_vet(s, uid="t:vet1", name="Dr. Test", online=True, verified=True, fee=199, langs=("en", "hi")):
    s.add(User(id=uid, lang="en", role="vet", state={}))
    v = VetProfile(user_id=uid, code="VET-" + uid[-4:], name=name, council="Test Council", reg_no="R-1",
                   verified=verified, online=online, fee=fee, free_for_strays=True, langs=list(langs), species=["any"])
    s.add(v)
    s.commit()
    return v


def ask_vet(s, text="mera puppy 2 din se ulti kar raha hai, kha nahi raha"):
    talk(s, "web:o", button="lang:en")
    talk(s, "web:o", button="m:vet")
    talk(s, "web:o", button="v:who:pet")
    talk(s, "web:o", button="v:sp:dog")
    return talk(s, "web:o", image=photo(), text=text)


def test_precheck_shows_a_reviewed_card_and_offers_a_vet(s, monkeypatch):
    monkeypatch.setattr(ai, "vet_intake", intake())
    out = ask_vet(s)
    t = texts(out, "web:o")
    assert "Vomiting or loose motions" in t and "See a vet today" in t and "parvovirus" in t
    assert any(b[0].startswith("v:connect:") for o in out for b in o.buttons)
    assert "mg" not in t.lower(), "no doses ever"


def test_emergency_words_send_people_to_a_clinic_first(s, monkeypatch):
    monkeypatch.setattr(ai, "vet_intake", intake(urgency="watch", card="general"))
    out = ask_vet(s, text="kutta behosh ho gaya, saans nahi le raha")
    c = s.query(Consult).one()
    assert c.urgency == "emergency", "code red flags can only raise urgency"
    assert "1962" in texts(out, "web:o") and any(o.link for o in out)


def test_full_consult_with_a_verified_vet(s, monkeypatch):
    v = add_vet(s)
    monkeypatch.setattr(ai, "vet_intake", intake())
    ask_vet(s)
    c = s.query(Consult).one()
    out = talk(s, "web:o", button=f"v:connect:{c.id}")
    alert = [o for o in out if o.to == "t:vet1"][0]
    assert "Consult request C-" in alert.text and "Puppy vomiting" in alert.text, "the vet gets a one-page summary"
    out = talk(s, "t:vet1", button=f"v:accept:{c.id}")
    assert c.status == "active" and "Reg. R-1" in texts(out, "web:o")
    # live chat both ways
    out = talk(s, "web:o", text="he is very weak")
    assert out[0].to == "t:vet1" and "he is very weak" in out[0].text
    out = talk(s, "t:vet1", text="Please give only water for now and come to the clinic.")
    assert out[0].to == "web:o" and "Dr. Test" in out[0].text
    assert s.query(ConsultMessage).count() == 2
    # video
    out = talk(s, "web:o", button=f"v:video:{c.id}")
    assert all(o.link and o.link.startswith("https://meet.jit.si/StrayWatch-") for o in out) and len(out) == 2
    # the vet closes with a note; the owner gets it; a follow-up is scheduled
    talk(s, "t:vet1", button=f"v:note:{c.id}")
    out = talk(s, "t:vet1", text="ORS water, light food; clinic visit tomorrow for a parvo test.")
    assert c.status == "closed" and "parvo test" in texts(out, "web:o")
    c.followup_at = now() - timedelta(minutes=1)
    s.commit()
    out = engine.escalate_due(s)
    assert any(o.to == "web:o" and "Checking in" in o.text for o in out)


def test_unverified_or_offline_vets_are_never_offered(s, monkeypatch):
    add_vet(s, "t:vetA", "Dr. Unverified", verified=False)
    add_vet(s, "t:vetB", "Dr. Offline", online=False)
    monkeypatch.setattr(ai, "vet_intake", intake())
    ask_vet(s)
    c = s.query(Consult).one()
    out = talk(s, "web:o", button=f"v:connect:{c.id}")
    assert c.status == "waiting" and not [o for o in out if o.to.startswith("t:")]
    out = talk(s, "t:vetB", text="online")
    assert any(o.to == "t:vetB" and "Consult request" in o.text for o in out), "the waiting request reaches the first vet online"


def test_no_reply_moves_to_the_next_vet(s, monkeypatch):
    add_vet(s, "t:vet1", "Dr. One", fee=0)
    add_vet(s, "t:vet2", "Dr. Two", fee=299)
    monkeypatch.setattr(ai, "vet_intake", intake())
    ask_vet(s)
    c = s.query(Consult).one()
    out = talk(s, "web:o", button=f"v:connect:{c.id}")
    pick = [b for o in out for b in o.options if b[0].startswith("v:pick:")]
    assert len(pick) == 2 and "free" in pick[0][1]
    talk(s, "web:o", button=pick[0][0])
    c.requested_at = now() - timedelta(minutes=6)
    s.commit()
    out = engine.escalate_due(s)
    assert any(o.to == "t:vet2" and "Consult request" in o.text for o in out)


def test_food_checker_answers_only_from_the_reviewed_list(s):
    talk(s, "web:o", button="lang:en")
    talk(s, "web:o", button="v:food")
    talk(s, "web:o", button="v:fd:dog")
    assert "Don't feed" in texts(talk(s, "web:o", text="can I give chocolate?"), "web:o")
    assert "Okay" in texts(talk(s, "web:o", text="roti"), "web:o")
    assert "won't guess" in texts(talk(s, "web:o", text="dragon fruit"), "web:o")


def test_asking_for_a_doctor_in_hindi_starts_vet_care(s):
    talk(s, "web:o", button="lang:hi")
    out = talk(s, "web:o", text="मुझे डॉक्टर से बात करनी है")
    assert any(b[0] == "v:who:pet" for o in out for b in o.options)


def test_vet_links_with_code_and_toggles_online(s):
    s.add(VetProfile(code="VET-7777", name="Dr. New", verified=True))
    s.commit()
    talk(s, "web:v", button="lang:en")
    out = talk(s, "web:v", text="vet VET-7777")
    assert "Welcome" in texts(out, "web:v") and s.get(User, "web:v").role == "vet"
    talk(s, "web:v", text="online")
    assert s.query(VetProfile).filter_by(code="VET-7777").one().online


def test_vet_with_two_open_consults_replies_to_the_one_they_are_in(s, monkeypatch):
    add_vet(s)
    monkeypatch.setattr(ai, "vet_intake", intake())
    for owner in ("web:o", "web:p"):
        talk(s, owner, button="lang:en")
        talk(s, owner, button="m:vet")
        talk(s, owner, button="v:who:pet")
        talk(s, owner, button="v:sp:dog")
        talk(s, owner, image=photo(), text="puppy vomiting")
        c = s.query(Consult).filter_by(user_id=owner).one()
        talk(s, owner, button=f"v:connect:{c.id}")
        talk(s, "t:vet1", button=f"v:accept:{c.id}")
    out = talk(s, "t:vet1", text="hello")
    assert out[0].to == "web:p", "the vet's message goes to the consult they joined last"
