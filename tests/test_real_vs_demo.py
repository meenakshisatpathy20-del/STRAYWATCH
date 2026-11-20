"""Real use vs the demo page: a real report or consult never waits on a sample account, and the demo stays sealed."""
from app import ai, config, engine
from app.db import Case, User, VetProfile
from test_engine import LALPUR, photo, s, talk, texts  # noqa: F401
from test_lifecycle import desc, report
from test_vet import ask_vet, intake


def add_samples(s):
    s.add(User(id="demo:ngo", lang="en", role="ngo", lat=LALPUR[0] + 0.001, lon=LALPUR[1], state={}))
    s.add(User(id="demo:coord", lang="en", role="coordinator", lat=LALPUR[0], lon=LALPUR[1], state={}))
    s.add(User(id="demo:arjun", lang="en", is_volunteer=True, lat=LALPUR[0] + 0.001, lon=LALPUR[1], state={}))
    s.add(User(id="demo:vet2", lang="en", role="vet", state={}))
    s.add(VetProfile(user_id="demo:vet2", code="VET-1002", name="Dr. Sample", council="x", reg_no="x", verified=True,
                     online=True, fee=0, free_for_strays=True, langs=["en"], species=["any"], sample=True))
    s.query(User).filter(User.id.like("h:%")).delete()
    s.commit()


def hurt_dog(monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True, summary="A dog bleeding on the road."))


def test_real_report_never_goes_to_sample_accounts_and_says_call_1962(s, monkeypatch):
    add_samples(s)
    hurt_dog(monkeypatch)
    out = report(s)
    assert not [o for o in out if o.to.startswith("demo:")]
    c = s.query(Case).one()
    assert c.status == "unanswered" and not c.asked
    assert "has joined near you yet" in texts(out, "web:r") and "1962" in texts(out, "web:r")


def test_real_report_reaches_a_real_volunteer_nearby(s, monkeypatch):
    add_samples(s)
    s.add(User(id="web:vol", lang="en", is_volunteer=True, lat=LALPUR[0] + 0.002, lon=LALPUR[1], state={}))
    s.commit()
    hurt_dog(monkeypatch)
    out = report(s)
    assert [o.to for o in out if o.buttons and o.buttons[0][0].startswith("case:go")] == ["web:vol"]


def test_demo_page_uses_sample_responders_and_does_not_merge_with_real_cases(s, monkeypatch):
    add_samples(s)
    hurt_dog(monkeypatch)
    report(s, uid="web:real")
    tok = config.DEMO_RUN.set(True)
    try:
        out = report(s, uid="web:demo")
    finally:
        config.DEMO_RUN.reset(tok)
    assert s.query(Case).count() == 2, "a demo report must not merge into a real case"
    assert any(o.to.startswith("demo:") and o.buttons for o in out)


def test_real_consult_is_not_offered_sample_vets(s, monkeypatch):
    add_samples(s)
    monkeypatch.setattr(ai, "vet_intake", intake())
    ask_vet(s)
    out = talk(s, "web:o", button="v:connect:1")
    shown = texts(out) + str([o.options or o.buttons for o in out])
    assert "Dr. Sample" not in shown


def test_demo_consult_is_offered_the_sample_vet(s, monkeypatch):
    add_samples(s)
    monkeypatch.setattr(ai, "vet_intake", intake())
    tok = config.DEMO_RUN.set(True)
    try:
        ask_vet(s)
        out = talk(s, "web:o", button="v:connect:1")
    finally:
        config.DEMO_RUN.reset(tok)
    assert "Dr. Sample" in texts(out) + str([o.options or o.buttons for o in out])


def test_extra_words_after_the_photo_do_not_repeat_the_long_location_ask(s, monkeypatch):
    hurt_dog(monkeypatch)
    talk(s, "web:x", button="lang:en")
    talk(s, "web:x", button="m:sos")
    first = talk(s, "web:x", image=photo())
    again = talk(s, "web:x", text="it is bleeding and not moving")
    assert engine.LOCATION_ASK in texts(first) and texts(again) == engine.LOCATION_AGAIN
