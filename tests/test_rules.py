"""Urgency rules are plain code: same input -> same urgency, every time.

Scope: animals in trouble (injured, sick, trapped, dead) and the accidents they can cause. Healthy animals
standing, eating or wandering are not cases.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import CHECK, HIGH, LOW, Facts, decide, facts_from, ist_hour  # noqa: E402


def test_injured_animal_on_the_road_is_high_and_gets_care_first():
    d = decide(Facts(species="dog", condition="injured", on_road=True, road_type="minor"))
    assert d.level == HIGH and d.teams[:2] == ["ngo", "vet"] and d.hop_minutes == 3


def test_injured_cow_lying_in_a_main_road_lane_brings_cattle_team_and_police():
    d = decide(Facts(species="cow", condition="injured", on_road=True, blocking_lane=True, road_type="main", lying_still=True))
    assert d.level == HIGH
    assert d.teams == ["ngo", "vet", "municipal_cattle", "police", "volunteer"]


def test_healthy_cow_standing_in_traffic_is_not_a_case():
    d = decide(Facts(species="cow", condition="healthy", on_road=True, blocking_lane=True, road_type="main", hour_ist=9))
    assert d.level == LOW and not d.dispatch and d.teams == []


def test_healthy_dog_wandering_or_eating_is_not_a_case():
    assert decide(Facts(species="dog", condition="healthy", on_road=False)).level == LOW


def test_healthy_animal_resting_is_not_lying_still_trouble():
    assert decide(Facts(species="cat", condition="healthy", lying_still=True)).level == LOW


def test_animal_lying_in_a_lane_with_unclear_condition_is_treated_as_possibly_hurt():
    d = decide(Facts(species="dog", condition="unknown", lying_still=True, on_road=True, blocking_lane=True))
    assert d.level == HIGH and d.teams[0] == "ngo"


def test_injured_off_the_road_needs_care():
    d = decide(Facts(species="dog", condition="injured", on_road=False))
    assert d.level == CHECK and d.teams == ["ngo", "vet", "volunteer"] and d.hop_minutes == 10


def test_sick_animal_off_the_road_needs_care():
    assert decide(Facts(species="cow", condition="sick")).level == CHECK


def test_trapped_animal_is_high_even_off_the_road():
    assert decide(Facts(species="dog", condition="trapped")).level == HIGH


def test_dead_animal_on_the_road_is_high_for_removal():
    d = decide(Facts(species="dog", condition="dead", on_road=True, blocking_lane=True))
    assert d.level == HIGH and d.teams[0] == "municipal" and "police" in d.teams
    assert decide(Facts(species="cow", condition="dead", on_road=False)).teams[0] == "municipal_cattle"


def test_reporter_words_always_win_over_a_calm_photo():
    d = decide(Facts(species="dog", condition="healthy", reporter_says_hurt=True, on_road=True))
    assert d.level == HIGH and d.teams[0] == "ngo"
    assert decide(Facts(species="dog", condition="healthy", reporter_says_hurt=True)).level == CHECK


def test_same_input_same_output():
    f = Facts(species="cow", condition="sick", on_road=True, blocking_lane=False, road_type="minor", hour_ist=18)
    assert all(decide(f) == decide(f) for _ in range(50))


def test_facts_from_ai_description_and_ist_hour():
    at = datetime(2026, 11, 20, 3, 0)  # 03:00 UTC = 08:30 IST
    assert ist_hour(at) == 8
    f = facts_from({"species": "cow", "condition": "injured", "on_road": True, "blocking_lane": True,
                    "road_type": "minor"}, False, at)
    assert decide(f).level == HIGH
