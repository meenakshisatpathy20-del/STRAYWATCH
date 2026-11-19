"""Camera agent → Agent Room: an edge event opens a case, merges into a duplicate (raising urgency), lights a signboard."""
from app import ai, engine
from app.db import Case, CaseEvent, User
from test_engine import photo, s, talk  # noqa: F401
from test_lifecycle import desc

CAM = "ratu-rd-01"
EV = {"camera_id": CAM, "kind": "dwell", "species": "cow", "seconds_in_lane": 62, "track_id": 7}


def cam_team(s):
    c = engine.CAMERAS[CAM]
    s.add(User(id="t:cattle", lang="en", role="municipal_cattle", lat=c["lat"] + 0.003, lon=c["lon"], state={}))
    s.commit()


def test_camera_event_opens_a_high_case_for_the_cattle_team(s, monkeypatch):
    cam_team(s)
    monkeypatch.setattr(ai, "describe", desc(species="cow", summary="Cows standing in the lane."))
    case, outs = engine.camera_case(s, CAM, EV, photo())
    s.commit()
    assert case.source == "camera" and case.level == "HIGH" and case.triage["blocking_lane"]
    assert [o.to for o in outs if o.buttons][:1] == ["t:cattle"]
    assert not [o for o in outs if o.to.startswith("camera:")], "nobody messages the camera"
    first = s.query(CaseEvent).filter_by(case_id=case.id).order_by(CaseEvent.id).first()
    assert first.agent == "Camera" and "62 s" in first.detail


def test_lane_report_raises_urgency_of_a_calmer_duplicate(s, monkeypatch):
    cam_team(s)
    c = engine.CAMERAS[CAM]
    monkeypatch.setattr(ai, "describe", desc(species="cow", condition="sick", summary="A thin cow by the drain."))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo())
    talk(s, "web:r", lat=c["lat"], lon=c["lon"])
    first = s.query(Case).one()
    assert first.level == "CHECK"
    monkeypatch.setattr(ai, "describe", desc(species="cow", summary="A cow in the lane."))
    case, outs = engine.camera_case(s, CAM, EV, photo("white"))
    s.commit()
    assert case.id == first.id and s.query(Case).count() == 1, "merged, not a second rescue"
    assert case.level == "HIGH" and "municipal_cattle" in case.teams
    kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=case.id)]
    assert "merged" in kinds and "upgraded" in kinds
    out = engine.escalate_due(s)
    assert any(o.to == "t:cattle" for o in out), "the Watch agent moves straight to the cattle team"


def test_signboard_warns_drivers_while_a_case_is_open(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(species="cow"))
    boards = {b["camera_id"]: b for b in engine.signboards(s)}
    assert not boards[CAM]["show"]
    engine.camera_case(s, CAM, EV, photo())
    s.commit()
    boards = {b["camera_id"]: b for b in engine.signboards(s)}
    assert boards[CAM]["show"] and "CATTLE" in boards[CAM]["message_en"]
