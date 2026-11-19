"""Camera agent → Agent Room: an animal LYING in a lane opens a case; a healthy animal is only logged."""
from app import ai, engine
from app.db import Case, CaseEvent, Sighting, User
from test_engine import photo, s, talk  # noqa: F401
from test_lifecycle import desc

CAM = "ratu-rd-01"
LYING = {"camera_id": CAM, "kind": "lying", "species": "dog", "seconds_in_lane": 34, "track_id": 7}


def team(s, uid, role):
    c = engine.CAMERAS[CAM]
    s.add(User(id=uid, lang="en", role=role, lat=c["lat"] + 0.003, lon=c["lon"], state={}))
    s.commit()


def test_animal_lying_in_a_lane_opens_a_high_case_care_first(s, monkeypatch):
    team(s, "t:ngo", "ngo")
    team(s, "t:police", "police")
    monkeypatch.setattr(ai, "describe", desc(species="dog", condition="unknown", summary="A dog lying in the lane."))
    case, outs = engine.camera_case(s, CAM, LYING, photo())
    s.commit()
    assert case.source == "camera" and case.level == "HIGH" and case.triage["lying_still"]
    assert case.teams[0] == "ngo" and "police" in case.teams
    assert [o.to for o in outs if o.buttons][:1] == ["t:ngo"]
    assert not [o for o in outs if o.to.startswith("camera:")], "nobody messages the camera"
    first = s.query(CaseEvent).filter_by(case_id=case.id).order_by(CaseEvent.id).first()
    assert first.agent == "Camera" and "lying" in first.detail


def test_healthy_animal_standing_in_a_lane_is_only_logged(s, monkeypatch):
    team(s, "t:cattle", "municipal_cattle")
    monkeypatch.setattr(ai, "describe", desc(species="cow", condition="healthy", summary="Cows standing on the road."))
    case, outs = engine.camera_case(s, CAM, {**LYING, "kind": "still", "species": "cow"}, photo())
    s.commit()
    assert case is None and outs == [] and s.query(Case).count() == 0
    assert s.query(Sighting).filter_by(source="camera").count() == 1, "kept for the traffic hotspot map"


def test_camera_report_raises_urgency_of_a_calmer_duplicate(s, monkeypatch):
    team(s, "t:ngo", "ngo")
    c = engine.CAMERAS[CAM]
    monkeypatch.setattr(ai, "describe", desc(species="dog", condition="sick", summary="A thin dog by the drain."))
    talk(s, "web:r", button="lang:en")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo())
    talk(s, "web:r", lat=c["lat"], lon=c["lon"])
    first = s.query(Case).one()
    assert first.level == "CHECK"
    monkeypatch.setattr(ai, "describe", desc(species="dog", condition="sick", summary="The dog is now lying in the lane."))
    case, outs = engine.camera_case(s, CAM, LYING, photo("white"))
    s.commit()
    assert case.id == first.id and s.query(Case).count() == 1, "merged, not a second rescue"
    assert case.level == "HIGH" and "police" in case.teams
    kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=case.id)]
    assert "merged" in kinds and "upgraded" in kinds


def test_signboard_warns_drivers_while_an_injured_animal_is_on_the_road(s, monkeypatch):
    monkeypatch.setattr(ai, "describe", desc(species="dog", condition="injured"))
    boards = {b["camera_id"]: b for b in engine.signboards(s)}
    assert not boards[CAM]["show"]
    engine.camera_case(s, CAM, LYING, photo())
    s.commit()
    boards = {b["camera_id"]: b for b in engine.signboards(s)}
    assert boards[CAM]["show"] and "INJURED" in boards[CAM]["message_en"]


def test_hotspot_agent_finds_where_animals_keep_getting_hurt(s, monkeypatch):
    from datetime import timedelta
    from app.db import now
    c = engine.CAMERAS[CAM]
    for i in range(2):
        s.add(Case(reporter_id="x", lat=c["lat"], lon=c["lon"], status="closed", level="HIGH",
                   triage={"species": "dog", "condition": "injured", "on_road": True}, created=now() - timedelta(days=i)))
    s.commit()
    acts = engine.hotspot_actions(s)
    assert acts and acts[0]["place"] == "Ratu Road" and "hurt" in acts[0]["action"]
