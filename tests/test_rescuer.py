"""What a real rescuer needs: checked helpers, an open-cases list, a private chat with the reporter, transport."""
from app import ai, engine
from app.db import Case, CaseEvent, User
from test_engine import LALPUR, photo, s, talk, texts  # noqa: F401
from test_lifecycle import desc, report


def hurt_dog(monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(condition="injured", on_road=True, summary="A dog bleeding on the road."))


def btn(outs, to, prefix):
    return next(b[0] for o in outs if o.to == to for b in (o.buttons or []) if b[0].startswith(prefix))


def test_a_new_helper_is_checked_by_a_coordinator_before_getting_alerts(s, monkeypatch):
    s.add(User(id="web:coord", lang="en", role="coordinator", lat=LALPUR[0], lon=LALPUR[1], state={}))
    s.commit()
    talk(s, "web:new", button="lang:en")
    out = talk(s, "web:new", text="volunteer")
    assert "name" in texts(out)
    talk(s, "web:new", text="Ravi, Lalpur feeders")
    out = talk(s, "web:new", lat=LALPUR[0] + 0.001, lon=LALPUR[1])
    new = s.get(User, "web:new")
    assert new.approved is False and new.name == "Ravi, Lalpur feeders"
    assert "coordinator checks" in texts(out, "web:new")
    approve = btn(out, "web:coord", "vol:ok:")
    # not approved yet → no rescue alerts
    s.query(User).filter(User.id.like("h:%")).delete()
    s.commit()
    hurt_dog(monkeypatch)
    out = report(s)
    assert not [o for o in out if o.to == "web:new"]
    # a non-coordinator can't approve; the coordinator can
    assert "Only coordinators" in texts(talk(s, "web:new", button=approve))
    out = talk(s, "web:coord", button=approve)
    assert s.get(User, "web:new").approved is True and "approved" in texts(out, "web:new")


def test_open_cases_list_lets_a_free_helper_pick_up_an_unanswered_case(s, monkeypatch):
    s.query(User).filter(User.id.like("h:%")).delete()
    s.commit()
    hurt_dog(monkeypatch)
    report(s)
    c = s.query(Case).one()
    assert c.status == "unanswered"
    s.add(User(id="web:vol", lang="en", is_volunteer=True, approved=True, lat=LALPUR[0] + 0.003, lon=LALPUR[1], state={}))
    s.commit()
    out = talk(s, "web:vol", text="cases")
    take = btn(out, "web:vol", "case:go:")
    assert f"SW-{c.id}" in texts(out)
    out = talk(s, "web:vol", button=take)
    assert c.status == "assigned" and c.assigned_to == "web:vol"
    assert any(o.to == "web:r" and "on the way" in o.text for o in out)


def test_reporter_and_rescuer_can_message_without_sharing_numbers(s, monkeypatch):
    hurt_dog(monkeypatch)
    out = report(s)
    c = s.query(Case).one()
    out = talk(s, c.current_helper, button=f"case:go:{c.id}")
    rescuer = c.assigned_to
    chat = btn(out, rescuer, "case:chat:")
    talk(s, rescuer, button=chat)
    out = talk(s, rescuer, text="I'm at the chowk, where exactly?")
    assert [o.to for o in out] == ["web:r"] and "Rescuer" in out[0].text and "where exactly" in out[0].text
    reply = btn(out, "web:r", "case:chat:")
    talk(s, "web:r", button=reply)
    out = talk(s, "web:r", lat=LALPUR[0] + 0.0004, lon=LALPUR[1])
    assert out[0].to == rescuer and "maps.google" in out[0].link
    assert "Chat ended" in texts(talk(s, "web:r", text="stop"))
    assert s.query(CaseEvent).filter(CaseEvent.kind == "message").count() == 2


def test_rescuer_can_ask_for_transport_and_a_partner_answers(s, monkeypatch):
    s.add(User(id="web:ngo", lang="en", role="ngo", lat=LALPUR[0] + 0.01, lon=LALPUR[1], state={}))
    s.commit()
    hurt_dog(monkeypatch)
    report(s)
    c = s.query(Case).one()
    talk(s, "h:a", button=f"case:go:{c.id}") if c.current_helper == "h:a" else None
    c.assigned_to, c.status = "h:a", "assigned"
    s.commit()
    assert "Only the rescuer" in texts(talk(s, "h:b", button=f"case:transport:{c.id}"))
    out = talk(s, "h:a", button=f"case:transport:{c.id}")
    assert "1962" in texts(out, "h:a")
    lift = btn(out, "web:ngo", "case:lift:")
    out = talk(s, "web:ngo", button=lift)
    assert any(o.to == "h:a" and "transport" in o.text for o in out)
    assert "already" in texts(talk(s, "web:ngo", button=lift))
