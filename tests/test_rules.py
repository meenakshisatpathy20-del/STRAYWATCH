"""Urgency rules (deck slide 7) are plain code: same input -> same urgency, every time."""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import CHECK, HIGH, LOW, Facts, decide, facts_from, ist_hour  # noqa: E402


def test_lane_blocked_on_main_road_is_high_and_calls_cattle_team_and_police():
    d = decide(Facts(species="cow", condition="healthy", on_road=True, blocking_lane=True, road_type="main", hour_ist=14))
    assert d.level == HIGH
    assert d.teams[:2] == ["municipal_cattle", "police"]
    assert d.hop_minutes == 3


def test_lane_blocked_at_peak_hour_is_high_even_on_minor_road():
    assert decide(Facts(species="cow", on_road=True, blocking_lane=True, road_type="minor", hour_ist=9)).level == HIGH


def test_minor_road_off_peak_blockage_is_check():
    d = decide(Facts(species="cow", condition="healthy", on_road=True, blocking_lane=True, road_type="minor", hour_ist=14))
    assert d.level == CHECK and d.hop_minutes == 10


def test_aggressive_dog_near_traffic_is_high_and_goes_to_abc_team():
    d = decide(Facts(species="dog", aggressive=True, on_road=True, hour_ist=14))
    assert d.level == HIGH and "abc" in d.teams


def test_injured_off_road_is_check_and_goes_to_ngo_first():
    d = decide(Facts(species="dog", condition="injured", on_road=False, hour_ist=14))
    assert d.level == CHECK and d.teams[0] == "ngo"


def test_lying_still_is_check():
    assert decide(Facts(species="dog", condition="unknown", lying_still=True, hour_ist=14)).level == CHECK


def test_off_road_and_not_hurt_is_low_and_nobody_is_dispatched():
    d = decide(Facts(species="cow", condition="healthy", on_road=False, hour_ist=9))
    assert d.level == LOW and d.teams == [] and not d.dispatch


def test_reporter_words_always_win():
    d = decide(Facts(species="dog", condition="healthy", reporter_says_hurt=True, hour_ist=14))
    assert d.level == HIGH and d.teams[0] == "ngo"


def test_hurt_on_road_is_always_high():
    assert decide(Facts(species="dog", condition="injured", on_road=True, road_type="minor", hour_ist=2)).level == HIGH


def test_same_input_same_output():
    f = Facts(species="cow", condition="sick", on_road=True, blocking_lane=False, road_type="minor", hour_ist=18)
    assert all(decide(f) == decide(f) for _ in range(50))


def test_facts_from_ai_description_and_ist_hour():
    # 03:00 UTC is 08:30 IST: peak hour
    at = datetime(2026, 11, 20, 3, 0)
    assert ist_hour(at) == 8
    f = facts_from({"species": "cow", "condition": "healthy", "on_road": True, "blocking_lane": True,
                    "road_type": "minor"}, False, at)
    assert decide(f).level == HIGH


def test_a_healthy_animal_resting_is_not_lying_still():
    assert decide(Facts(species="cat", condition="healthy", lying_still=True, hour_ist=14)).level == LOW
