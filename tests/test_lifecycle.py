"""Case lifecycle from the deck (slide 5): rules → caretaker/right team → escalation → coordinator → photo-proof close."""
from datetime import timedelta

from app import ai, engine
from app.db import Case, CaseEvent, User, now
from test_engine import LALPUR, photo, s, talk, texts  # noqa: F401  (s is the shared DB fixture)


def desc(**kw):
    base = {"is_animal": True, "species": "dog", "count": 1, "colour": "tan", "markings": "", "ear_notch": False,
            "collar": "none", "ear_tag": False, "sex_guess": "unknown", "age_guess": "adult", "condition": "healthy",
            "injuries": "none visible", "on_road": False, "blocking_lane": False, "road_type": "unknown",
            "lying_still": False, "aggressive": False, "urgency": "none", "first_aid_card": "general",
            "summary": "An animal.", "confidence": 0.95, "ai_ok": True}
    base.update(kw)
    return lambda *a, **k: dict(base)


def add_team(s, uid, role, dlat=0.002):
    s.add(User(id=uid, lang="en", role=role, lat=LALPUR[0] + dlat, lon=LALPUR[1], state={}))
    s.commit()


def report(s, uid="web:r", text=""):
    talk(s, uid, button="lang:en")
    talk(s, uid, button="m:sos")
    talk(s, uid, image=photo(), text=text)
    return talk(s, uid, lat=LALPUR[0], lon=LALPUR[1])


def test_cow_blocking_main_road_goes_to_cattle_team_first(s, monkeypatch):
    add_team(s, "t:cattle", "municipal_cattle")
    add_team(s, "t:police", "police")
    monkeypatch.setattr(ai, "describe", desc(species="cow", on_road=True, blocking_lane=True, road_type="main",
                                             summary="A cow standing in the middle lane."))
    out = report(s)
    c = s.query(Case).one()
    assert c.level == "HIGH" and c.teams[:2] == ["municipal_cattle", "police"]
    assert [o.to for o in out if o.buttons and o.buttons[0][0].startswith("case:go")] == ["t:cattle"]
    assert "URGENT" in texts(out, "web:r")


def test_calm_animal_off_road_is_only_logged(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(species="cow", summary="A cow resting by a wall."))
    out = report(s)
    c = s.query(Case).one()
    assert c.status == "logged" and c.level == "LOW"
    assert not [o for o in out if o.to != "web:r"], "nobody is disturbed for a LOW case"
    # ...but the citizen can say it got worse, and then responders are alerted
    out = talk(s, "web:r", button=f"case:worse:{c.id}")
    assert c.status == "alerting" and c.level == "HIGH"
    assert any(o.to == "demo:a" for o in out)


def test_escalates_to_duty_coordinator_after_everyone_else(s, monkeypatch):
    for uid in ("demo:a", "demo:b"):
        s.get(User, uid).is_volunteer = False  # no volunteers: only the coordinator is left
    add_team(s, "t:coord", "coordinator", dlat=0.05)
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    out = report(s)
    assert any(o.to == "t:coord" and "coordinator" in o.text.lower() for o in out)
    c = s.query(Case).one()
    c.alerted_at = now() - timedelta(minutes=20)
    s.commit()
    out = engine.escalate_due(s)
    assert c.status == "unanswered"
    assert any(o.to == "web:r" and "1962" in o.text for o in out), "reporter gets the government ambulance number"


def test_closing_needs_a_photo_checked_by_ai(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    report(s)
    c = s.query(Case).one()
    talk(s, "demo:a", button=f"case:go:{c.id}")
    out = talk(s, "demo:a", button=f"case:safe:{c.id}")
    assert "photo" in texts(out, "demo:a") and c.status == "assigned", "not closed until a photo arrives"
    monkeypatch.setattr(ai, "proof_check", lambda *a, **k: {"ok": False, "checked": True, "note": "no animal visible"})
    out = talk(s, "demo:a", image=photo("gray"))
    assert c.status == "assigned" and "couldn't see" in texts(out, "demo:a")
    monkeypatch.setattr(ai, "proof_check", lambda *a, **k: {"ok": True, "checked": True, "note": "dog on a stretcher"})
    out = talk(s, "demo:a", image=photo("green"))
    assert c.status == "closed" and c.proof_photo_id
    assert any(o.to == "web:r" and o.image_url for o in out), "the reporter sees the proof photo"


def test_every_hand_off_is_in_the_ledger(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    report(s)
    c = s.query(Case).one()
    talk(s, "demo:a", button=f"case:go:{c.id}")
    kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=c.id).order_by(CaseEvent.id)]
    assert kinds[:5] == ["reported", "triaged", "ruled", "planned", "alerted"] and kinds[-1] == "accepted"


def test_reporter_saying_hurt_on_road_word_sets_on_road(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(species="dog"))
    report(s, text="gaadi ne maara, khoon aa raha hai")
    c = s.query(Case).one()
    assert c.level == "HIGH" and c.triage["on_road"]
