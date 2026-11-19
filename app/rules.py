"""Urgency and routing rules: plain, unit-tested code (deck slide 7: "AI reads and compares; tested rules decide").

The AI only fills in facts (species, injured?, on the road?, blocking a lane?). These functions turn facts into
an urgency level and the teams to ask, the same way every time: same input -> same urgency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

HIGH, CHECK, LOW = "HIGH", "CHECK", "LOW"
CATTLE = {"cow", "buffalo", "bull", "ox", "goat", "horse", "donkey", "pig"}
PEAK_HOURS_IST = [(8, 11), (17, 20)]  # 8-11 AM and 5-8 PM

# Who handles what. Teams are asked in this order after the animal's own caretaker.
TEAM_LABELS = {
    "caretaker": "the animal's caretaker",
    "ngo": "animal rescue NGO",
    "vet": "partner vet",
    "municipal_cattle": "municipal cattle team",
    "abc": "municipal ABC (dog) team",
    "police": "traffic police",
    "volunteer": "StrayWatch volunteer",
    "coordinator": "duty coordinator",
}

# Minutes each responder gets before the Watch agent asks the next one (deck slide 5: 3-10 min per hop).
HOP_MINUTES = {HIGH: 3, CHECK: 10, LOW: 0}


@dataclass
class Facts:
    species: str = "other"
    condition: str = "unknown"      # healthy | injured | sick | trapped | dead | unknown
    on_road: bool = False
    blocking_lane: bool = False
    road_type: str = "unknown"      # main | minor | none | unknown
    lying_still: bool = False
    aggressive: bool = False
    reporter_says_hurt: bool = False
    hour_ist: int = 12


@dataclass
class Decision:
    level: str
    reasons: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    hop_minutes: int = 0
    dispatch: bool = True


def is_peak(hour_ist: int) -> bool:
    return any(a <= hour_ist < b for a, b in PEAK_HOURS_IST)


def ist_hour(utc: datetime) -> int:
    return (utc + timedelta(hours=5, minutes=30)).hour


def decide(f: Facts) -> Decision:
    """Urgency per deck slide 7, plus one safety addition: a badly hurt, trapped or bleeding animal is always HIGH."""
    reasons: list[str] = []
    hurt = f.condition in ("injured", "trapped", "sick") or f.reporter_says_hurt
    cattle = f.species in CATTLE
    main_or_peak = f.road_type == "main" or is_peak(f.hour_ist)

    level = LOW
    if f.on_road and f.blocking_lane and main_or_peak:
        level = HIGH
        reasons.append("lane blocked on a main road" if f.road_type == "main" else "lane blocked at peak hour")
    if f.aggressive and f.on_road:
        level = HIGH
        reasons.append("aggressive animal near traffic")
    if f.condition == "trapped" or (hurt and f.on_road):
        level = HIGH
        reasons.append("hurt or trapped animal on the road" if f.on_road else "trapped animal")
    if f.reporter_says_hurt and level != HIGH:
        level = HIGH
        reasons.append("the person on the spot says it is hurt")
    still = f.lying_still and f.condition != "healthy"  # a relaxed, resting animal is not "lying still"
    if level == LOW and (hurt or still):
        level = CHECK
        reasons.append("injured, sick or lying still" if hurt else "lying still")
    if level == LOW and f.on_road and f.blocking_lane:
        level = CHECK
        reasons.append("blocking a minor road off-peak")
    if level == LOW and f.on_road:
        level = CHECK
        reasons.append("on the road")
    if f.condition == "dead":
        level = CHECK
        reasons = ["dead animal: needs removal"]
    if level == LOW:
        reasons.append("off the road and not hurt")

    return Decision(level=level, reasons=reasons, teams=teams_for(f, level), hop_minutes=HOP_MINUTES[level],
                    dispatch=level != LOW)


def teams_for(f: Facts, level: str) -> list[str]:
    """The right teams, in order. The caretaker (if the animal is known) is always added in front by the engine."""
    if level == LOW:
        return []
    hurt = f.condition in ("injured", "trapped", "sick") or f.reporter_says_hurt
    cattle = f.species in CATTLE
    if f.condition == "dead":
        return ["municipal_cattle" if cattle else "abc", "volunteer"]
    teams: list[str] = []
    if hurt:
        teams += ["ngo", "vet"]
    if cattle and f.on_road:
        teams += ["municipal_cattle"]
    if f.aggressive and not cattle:
        teams += ["abc"]
    if f.on_road and f.blocking_lane and level == HIGH:
        teams += ["police"]
    teams += ["volunteer"]
    return list(dict.fromkeys(teams))


def facts_from(desc: dict, reporter_says_hurt: bool, at_utc: datetime) -> Facts:
    """Build rule inputs from the validated AI description."""
    return Facts(
        species=str(desc.get("species") or "other"),
        condition=str(desc.get("condition") or "unknown"),
        on_road=bool(desc.get("on_road")),
        blocking_lane=bool(desc.get("blocking_lane")),
        road_type=str(desc.get("road_type") or "unknown"),
        lying_still=bool(desc.get("lying_still")),
        aggressive=bool(desc.get("aggressive")),
        reporter_says_hurt=reporter_says_hurt,
        hour_ist=ist_hour(at_utc),
    )
