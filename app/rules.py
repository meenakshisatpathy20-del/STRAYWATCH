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
    "municipal": "municipal animal team",
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
    """Urgency for the refined problem: animals in trouble, and the accidents they can cause.

    * injured / sick / trapped animal ON the road -> HIGH (animal welfare + accident risk)
    * dead animal on the road -> HIGH (removal before it causes a crash); off the road -> CHECK
    * injured / sick / trapped off the road -> CHECK (needs care)
    * animal lying still IN a traffic lane, condition unclear -> treated as possibly hurt
    * healthy animal standing, eating or wandering (on the road or not) -> LOW: no case, nobody dispatched
    """
    reasons: list[str] = []
    cattle = f.species in CATTLE
    lying_unclear = f.lying_still and f.condition in ("unknown", "sick", "injured")
    hurt = f.condition in ("injured", "trapped", "sick") or f.reporter_says_hurt or lying_unclear
    if f.condition == "dead":
        level = HIGH if f.on_road else CHECK
        reasons.append("dead animal on the road: remove it before it causes an accident" if f.on_road
                       else "dead animal: needs removal")
    elif hurt and f.on_road:
        level = HIGH
        reasons.append("hurt animal on the road: it needs care, and it can cause an accident")
        if f.reporter_says_hurt and f.condition not in ("injured", "trapped", "sick"):
            reasons.append("the person on the spot says it is hurt")
    elif hurt:
        level = CHECK
        reasons.append("the person on the spot says it is hurt" if f.reporter_says_hurt and f.condition not in
                       ("injured", "trapped", "sick") else ("trapped animal" if f.condition == "trapped" else
                                                            "injured or sick animal: needs care"))
        if f.condition == "trapped":
            level = HIGH
    elif f.lying_still and f.on_road and f.blocking_lane:
        level = CHECK
        reasons.append("animal lying in a traffic lane: may be hurt, and an accident risk")
    else:
        level = LOW
        reasons.append("healthy animal on the road: no rescue needed, logged for the hotspot map" if f.on_road
                       else "healthy animal: no rescue needed")
    return Decision(level=level, reasons=reasons, teams=teams_for(f, level, hurt), hop_minutes=HOP_MINUTES[level],
                    dispatch=level != LOW)


def teams_for(f: Facts, level: str, hurt: bool | None = None) -> list[str]:
    """The right teams, in order. The caretaker (if the animal is known) is always added in front by the engine."""
    if level == LOW:
        return []
    cattle = f.species in CATTLE
    big_road = f.on_road and (f.blocking_lane or f.road_type == "main")
    teams: list[str] = []
    if f.condition == "dead":
        teams += ["municipal_cattle" if cattle else "municipal"]
    else:
        if hurt is None or hurt:
            teams += ["ngo", "vet"]
        if cattle and f.on_road:
            teams += ["municipal_cattle"]  # a big animal needs several people to move safely
        if not hurt and not cattle:
            teams += ["ngo"]
    if big_road:
        teams += ["police"]  # guard the spot so no vehicle hits the animal or the rescuer
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
