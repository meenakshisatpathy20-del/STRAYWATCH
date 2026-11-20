"""Vet care hub data: appointment slots, the nutrition shop catalogue and vaccination schedules.

Schedules are general guidance from Indian veterinary practice and the government's NADCP programme; the page always
says the vet decides. Shop prices are indicative pilot prices for generic products (no brands)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_TIMES = ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30"]
DAYS_AHEAD = 7
MODES = {"video": "Video consult", "clinic": "Clinic visit"}
SPECIES = {"dog": "Dog", "cat": "Cat", "cow": "Cow / cattle", "other": "Other"}


def code(prefix: str) -> str:
    return prefix + "-" + "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(5))


def now_ist() -> datetime:
    return datetime.now(IST)


def open_days() -> list[tuple[str, str]]:
    """(YYYY-MM-DD, 'Fri 21 Nov') for the next week. Sunday is closed."""
    out, d = [], now_ist().date()
    for i in range(DAYS_AHEAD + 1):
        day = d + timedelta(days=i)
        if day.weekday() != 6:
            out.append((day.isoformat(), ("Today" if i == 0 else "Tomorrow" if i == 1 else day.strftime("%a")) + day.strftime(" %d %b")))
    return out[:DAYS_AHEAD]


def free_slots(day: str, taken: set[str]) -> list[str]:
    """Slots on `day` not booked yet and at least 30 minutes away."""
    cutoff = (now_ist() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    return [t for t in SLOT_TIMES if t not in taken and f"{day} {t}" > cutoff]


def nice_slot(day: str, slot: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    h, m = map(int, slot.split(":"))
    return d.strftime("%a %d %b") + f", {(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


# ------------------------------------------------------------------ nutrition shop (generic products, no brands)

PRODUCTS = [
    # id, animal, name, size, price ₹, emoji, note
    ("p-puppy", "dog", "Puppy starter food", "1.2 kg", 349, "🐶", "Small kibble for 1–12 month puppies. Soak in water for young pups."),
    ("p-dog", "dog", "Adult dog dry food", "3 kg", 749, "🐕", "Complete daily food for adult dogs. About 1 month for a 15 kg dog."),
    ("p-dogwet", "dog", "Dog wet food pouches", "6 × 70 g", 294, "🥫", "Soft food for old, sick or recovering dogs who won't eat dry food."),
    ("p-kitten", "cat", "Kitten food", "1 kg", 399, "🐱", "For kittens up to 1 year. Higher protein for growth."),
    ("p-cat", "cat", "Adult cat dry food", "1.2 kg", 449, "🐈", "Complete food for adult cats. Always keep water nearby."),
    ("p-catwet", "cat", "Cat wet food pouches", "6 × 70 g", 270, "🥫", "Helps cats who drink little water."),
    ("p-mineral", "cow", "Cattle mineral mixture", "1 kg", 180, "🐄", "Daily mineral supplement recommended for cattle. Mix in feed."),
    ("p-cattlefeed", "cow", "Balanced cattle feed", "10 kg", 320, "🌾", "Concentrate feed. Never give cows food in plastic bags."),
    ("p-ors", "all", "Pet oral rehydration salts", "10 sachets", 120, "💧", "For mild loose motions while you reach a vet. Not a treatment."),
    ("p-bowl", "all", "Steel food + water bowl set", "2 bowls", 199, "🥣", "Heavy bowls that don't tip over. Good for street feeding spots."),
    ("p-comb", "all", "Tick & flea comb", "1", 99, "🪮", "Check your animal every day in the monsoon."),
    ("p-feedpack", "stray", "Street dog feeding pack", "10 kg dry food", 1499, "💛",
     "Feeds about 10 street dogs for a week. Delivered to a StrayWatch volunteer feeder near you."),
]
PRODUCT = {p[0]: p for p in PRODUCTS}
ANIMALS = [("all", "All"), ("dog", "🐶 Dogs"), ("cat", "🐱 Cats"), ("cow", "🐄 Cows"), ("stray", "💛 Feed a street animal")]
DELIVERY_FREE_FROM, DELIVERY_FEE = 499, 40


def price_cart(raw: dict) -> tuple[list[dict], int, int]:
    """Server-side pricing from the catalogue; quantities clamped to 1–10. Returns (items, delivery, total)."""
    items = []
    for pid, qty in (raw or {}).items():
        p = PRODUCT.get(str(pid))
        try:
            q = max(0, min(10, int(qty)))
        except (TypeError, ValueError):
            q = 0
        if p and q:
            items.append({"id": p[0], "name": f"{p[2]} ({p[3]})", "qty": q, "price": p[4]})
    sub = sum(i["qty"] * i["price"] for i in items)
    delivery = 0 if sub >= DELIVERY_FREE_FROM or not items else DELIVERY_FEE
    return items, delivery, sub + delivery


# ------------------------------------------------------------------ vaccination schedules (guidance; the vet decides)

VACCINES = {
    "dog": [("6–8 weeks", "First puppy vaccine (DHPPi + L) · start deworming every 2 weeks"),
            ("9–12 weeks", "Second puppy vaccine (booster)"),
            ("12–16 weeks", "Third booster · first anti-rabies shot"),
            ("Every year", "Booster + anti-rabies · deworm every 3 months")],
    "cat": [("8 weeks", "First kitten vaccine (FVRCP) · deworm"),
            ("12 weeks", "FVRCP booster · first anti-rabies shot"),
            ("16 weeks", "Final kitten booster"),
            ("Every 1–3 years", "Booster + anti-rabies, as your vet advises")],
    "cow": [("Every 6 months", "Foot-and-mouth disease (FMD): free at government camps (NADCP)"),
            ("Before the monsoon", "HS and black-quarter (BQ) vaccines, every year"),
            ("Female calves, 4–8 months", "Brucellosis, once: free under NADCP"),
            ("Every year", "Lumpy skin disease (LSD) vaccine")],
}
