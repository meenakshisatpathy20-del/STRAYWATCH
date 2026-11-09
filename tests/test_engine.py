"""Engine tests with a stand-in AI, so they run offline: python -m pytest -q"""
import io
import os
import sys
from datetime import timedelta
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SW_ESCALATE_AFTER"] = "180"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app import ai, engine  # noqa: E402
from app.db import Base, Case, LostPost, SessionLocal, User, engine as db_engine, now  # noqa: E402
from app.engine import Incoming  # noqa: E402

LALPUR = (23.3725, 85.3330)


def photo(color="brown"):
    b = io.BytesIO()
    Image.new("RGB", (300, 200), color).save(b, "JPEG")
    return b.getvalue()


def fake_describe(species="dog", condition="healthy", urgency="none", is_animal=True):
    def f(s, uid, image, message=""):
        return {"is_animal": is_animal, "species": species, "count": 1, "colour": "tan", "markings": "white chest",
                "ear_notch": False, "collar": "none", "ear_tag": False, "sex_guess": "unknown", "age_guess": "adult",
                "condition": condition, "injuries": "none visible", "on_road": False, "urgency": urgency,
                "first_aid_card": "general", "summary": f"A {species}.", "confidence": 0.95, "ai_ok": True}
    return f


@pytest.fixture()
def s(monkeypatch):
    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    monkeypatch.setattr(ai, "match", lambda *a, **k: {"key": None, "confidence": 0.0, "reason": "test"})
    monkeypatch.setattr(ai, "compare_health", lambda *a, **k: {"changed": False, "note": "", "see_vet": False})
    monkeypatch.setattr(ai, "intent", lambda s, u, t: {"intent": "other", "name": None, "via": "test"})
    with SessionLocal() as sess:
        for uid, dlat in (("demo:a", 0.001), ("demo:b", 0.01), ("demo:far", 1.0)):
            sess.add(User(id=uid, lang="en", is_volunteer=True, lat=LALPUR[0] + dlat, lon=LALPUR[1], state={}))
        sess.commit()
        yield sess


def talk(s, uid, **kw):
    out = engine.handle(s, uid, Incoming(**kw))
    s.commit()
    return out


def texts(outs, to=None):
    return " | ".join(o.text for o in outs if to is None or o.to == to)


def test_language_first_then_menu(s):
    out = talk(s, "web:1", text="hi")
    assert out[0].options and out[0].options[1][0] == "lang:hi"
    out = talk(s, "web:1", button="lang:en")
    assert [b[0] for b in out[0].buttons] == ["m:sos", "m:animals", "m:lost"]


def test_sos_alerts_nearest_helper_and_escalates(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(condition="injured", urgency="high"))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo())
    out = talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    assert "URGENT" in texts(out, "web:r")
    first = [o for o in out if o.to == "demo:a"]
    assert first and first[0].buttons[0][0].startswith("case:go")
    assert not [o for o in out if o.to == "demo:far"], "helpers beyond the radius must not be alerted"
    case = s.query(Case).one()
    case.alerted_at = now() - timedelta(minutes=5)  # nobody answered in time
    s.commit()
    out = engine.escalate_due(s)
    assert any(o.to == "demo:b" for o in out), "should move on to the next helper"
    assert any(o.to == "web:r" and "Still finding help" in o.text for o in out), "reporter is kept informed"


def test_reporter_words_override_a_calm_photo(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(condition="healthy", urgency="none"))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo(), text="gaadi ne maara, khoon aa raha hai")
    out = talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    assert "URGENT" in texts(out, "web:r")
    assert "Hurt by a vehicle" in texts(out, "web:r")


def test_photo_without_animal_alerts_nobody(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(is_animal=False))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo("blue"))
    out = talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    assert all(o.to == "web:r" for o in out)
    assert "couldn't see an animal" in texts(out)


def test_accept_notifies_reporter_and_second_accept_is_polite(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(condition="injured", urgency="high"))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo())
    talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    cid = s.query(Case).one().id
    out = talk(s, "demo:a", button=f"case:go:{cid}")
    assert any(o.to == "web:r" and "on the way" in o.text for o in out)
    assert any(o.link and "maps.google" in o.link for o in out if o.to == "demo:a"), "helper gets directions"
    out = talk(s, "demo:b", button=f"case:go:{cid}")
    assert "already on the way" in texts(out)


def test_lost_poster_hides_owner_and_exact_spot(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(species="cow"))
    talk(s, "web:o", button="lang:en")
    talk(s, "web:o", button="l:lost")
    talk(s, "web:o", image=photo("white"))
    talk(s, "web:o", text="Gauri")
    talk(s, "web:o", lat=23.39051234, lon=85.30509876)
    talk(s, "web:o", text="yesterday evening")
    out = talk(s, "web:o", button="lost:public")
    post = s.query(LostPost).one()
    assert post.public and any(o.link and post.code in o.link for o in out)
    from app.main import post_card
    card = post_card(post)
    assert "web:o" not in str(card)
    assert "23.39051234" not in str(card) and "85.30509876" not in str(card)


def test_duplicate_sos_is_merged(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", fake_describe(condition="injured", urgency="high"))
    for uid in ("web:r1", "web:r2"):
        talk(s, uid, button="lang:en")
        talk(s, uid, button="m:sos")
        talk(s, uid, image=photo())
        out = talk(s, uid, lat=LALPUR[0], lon=LALPUR[1])
    assert s.query(Case).count() == 1
    assert "already reported" in texts(out, "web:r2")
    assert s.query(Case).one().merged_reports == 2


def test_helper_daily_cap(s, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_ALERTS_PER_HELPER_PER_DAY", 1)
    s.add(Case(reporter_id="x", lat=LALPUR[0], lon=LALPUR[1], asked=["demo:a"], triage={}, status="closed"))
    s.commit()
    assert "demo:a" not in engine.pick_helpers(s, LALPUR[0], LALPUR[1], exclude=set())


def test_parse_json_handles_fences_and_chatter():
    assert ai.parse_json('Sure!\n```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}


def test_change_of_mind_mid_flow(s, monkeypatch):
    monkeypatch.setattr(ai, "intent", lambda s_, u, t: {"intent": "lost", "name": None, "via": "test"})
    talk(s, "web:u", button="lang:en")
    talk(s, "web:u", button="l:seen")
    out = talk(s, "web:u", text="meri billi kho gayi hai")
    assert "Let's find them together" in texts(out)


def test_emergency_interrupts_other_flows(s, monkeypatch):
    monkeypatch.setattr(ai, "intent", lambda s_, u, t: {"intent": "sos", "name": None, "via": "test"})
    talk(s, "web:u", button="lang:en")
    talk(s, "web:u", button="a:checkin")
    out = talk(s, "web:u", text="ek kutta ghayal hai yahan")
    assert "location" in texts(out).lower()
    assert s.get(User, "web:u").state.get("flow") == "sos"
