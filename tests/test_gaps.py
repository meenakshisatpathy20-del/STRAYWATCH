"""Fixes from the real-world audit: typed landmarks, spam limit, cancelling, phone notifications, translated tracking."""
from app import ai, config, engine, geo, main, push
from app.db import Case, PushSub, User
from test_engine import LALPUR, photo, s, talk, texts  # noqa: F401
from test_lifecycle import desc


def start_sos(s, uid="web:r", text=""):
    talk(s, uid, button="lang:en")
    talk(s, uid, button="m:sos")
    return talk(s, uid, image=photo(), text=text)


def test_typed_landmark_is_confirmed_before_use(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    out = start_sos(s)
    assert "landmark" in texts(out, "web:r")
    monkeypatch.setattr(geo, "geocode", lambda q, near=None: {"lat": LALPUR[0], "lon": LALPUR[1], "label": "Lalpur Chowk, Ranchi"})
    out = talk(s, "web:r", text="Lalpur chowk ranchi")
    assert "Is it near" in texts(out, "web:r") and s.query(Case).count() == 0, "never used without confirming"
    out = talk(s, "web:r", button="sos:locyes")
    c = s.query(Case).one()
    assert abs(c.lat - LALPUR[0]) < 1e-6 and "URGENT" in texts(out, "web:r")


def test_unknown_landmark_asks_again_and_injury_words_are_not_treated_as_places(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured"))
    start_sos(s)
    called = []
    monkeypatch.setattr(geo, "geocode", lambda q, near=None: called.append(q) or None)
    out = talk(s, "web:r", text="khoon aa raha hai")
    assert not called, "an injury description is not a place"
    out = talk(s, "web:r", text="somewhere unknown xyz")
    assert called and "couldn't find" in texts(out, "web:r") and s.query(Case).count() == 0


def test_reporter_limit_stops_spam(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    monkeypatch.setattr(config, "MAX_SOS_PER_HOUR", 2)
    for i in range(3):
        start_sos(s)
        out = talk(s, "web:r", lat=LALPUR[0] + i * 0.01, lon=LALPUR[1])
    assert s.query(Case).count() == 2 and "1962" in texts(out, "web:r")


def test_reporter_can_cancel_and_the_responder_stands_down(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    start_sos(s)
    out = talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    c = s.query(Case).one()
    assert any(b[0] == f"case:cancel:{c.id}" for o in out for b in o.buttons if o.to == "web:r")
    out = talk(s, "web:r", button=f"case:cancel:{c.id}")
    assert c.status == "cancelled" and any(o.to == "demo:a" and "cancelled" in o.text for o in out)
    assert engine.escalate_due(s) == [], "nobody else is alerted after cancelling"


def test_alerts_reach_subscribed_phones_once_per_update(s, monkeypatch):
    s.add(User(id="web:h", lang="en", is_volunteer=True, lat=LALPUR[0] + 0.0002, lon=LALPUR[1], state={}))
    s.add(PushSub(user_id="web:h", endpoint="https://push.example/abc", p256dh="k", auth="a"))
    s.commit()
    sent = []
    monkeypatch.setattr(push, "notify", lambda uid, text, url="/app": sent.append((uid, text, url)))
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    start_sos(s)
    outs = engine.handle(s, "web:r", engine.Incoming(lat=LALPUR[0], lon=LALPUR[1]))
    main.deliver(s, outs, viewer="web:r")
    assert [u for u, *_ in sent] == ["web:h"], "one notification for the nearest subscribed helper, none for demo users"


def test_tracking_page_speaks_the_reporters_language(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True))
    talk(s, "web:r", button="lang:hi")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo())
    talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    monkeypatch.setattr(ai, "translate_batch", lambda texts, lang: ["HI:" + t for t in texts])
    c = s.query(Case).one()
    tr = engine.translate_texts(s, ["Every step, as it happened", main.case_view(s, c)["headline"]], "hi")
    assert all(v.startswith("HI:") for v in tr.values())
