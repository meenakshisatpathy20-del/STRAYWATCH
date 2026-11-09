"""Demo data so the website works for judges without real people.

Everything seeded here is clearly marked as sample data on the site. Photos are openly licensed (see
data/demo_photos/credits.json). Locations are around Ranchi, where StrayWatch started.
"""
from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps
from sqlalchemy import select

from . import photos
from .db import Animal, LostPost, SessionLocal, Sighting, User, now

PHOTOS = Path(__file__).resolve().parent.parent / "data" / "demo_photos"
SAMPLES = Path(__file__).resolve().parent / "static" / "samples"

LALPUR = (23.3725, 85.3330)
KANKE_RD = (23.3950, 85.3200)
RATU_RD = (23.3905, 85.3050)


def _jpg(path: Path) -> bytes:
    return path.read_bytes()


def _variant(path: Path, flip=True, dark=0.8, crop=0.08) -> bytes:
    """The 'same animal, another day' photo used by the try-it samples."""
    im = Image.open(path)
    w, h = im.size
    im = im.crop((int(w * crop), int(h * crop / 2), int(w * (1 - crop)), int(h * (1 - crop / 3))))
    if flip:
        im = ImageOps.mirror(im)
    im = ImageEnhance.Brightness(im).enhance(dark)
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=85)
    return b.getvalue()


def make_samples():
    SAMPLES.mkdir(parents=True, exist_ok=True)
    jobs = {"sos_bruno.jpg": ("dog_4.jpg", True), "register_dog.jpg": ("dog_6.jpg", False),
            "lost_cow.jpg": ("cow_7.jpg", True), "checkin_bruno.jpg": ("dog_4.jpg", False)}
    for name, (src, var) in jobs.items():
        out = SAMPLES / name
        if not out.exists():
            data = _variant(PHOTOS / src, flip=True, dark=0.85) if var else _variant(PHOTOS / src, flip=False, dark=1.0, crop=0.03)
            out.write_bytes(photos.normalise(data))


def seed():
    make_samples()
    with SessionLocal() as s:
        if s.get(User, "demo:priya"):
            return
        users = [
            User(id="demo:priya", name="Priya", lang="hi", is_volunteer=True, lat=LALPUR[0] + 0.0010, lon=LALPUR[1] + 0.0008, state={}),
            User(id="demo:arjun", name="Arjun", lang="en", is_volunteer=True, lat=LALPUR[0] - 0.0090, lon=LALPUR[1] - 0.0060, state={}),
            User(id="demo:farida", name="Farida", lang="bn", is_volunteer=True, lat=KANKE_RD[0], lon=KANKE_RD[1], state={}),
            User(id="demo:ramesh", name="Ramesh", lang="hi", lat=RATU_RD[0], lon=RATU_RD[1], state={}),
            User(id="demo:sunita", name="Sunita", lang="hi", lat=RATU_RD[0] + 0.01, lon=RATU_RD[1] + 0.01, state={}),
            User(id="demo:neha", name="Neha", lang="en", lat=LALPUR[0], lon=LALPUR[1], state={}),
        ]
        s.add_all(users)

        def ph(fname):
            return photos.save(s, _jpg(PHOTOS / fname)).id

        bruno_p = ph("dog_4.jpg")
        s.add(Animal(name="Bruno", species="dog", photo_id=bruno_p, lat=LALPUR[0], lon=LALPUR[1], guardians=["demo:priya"],
                     rabies_vaccinated_on=(now() - timedelta(days=200)).date().isoformat(),
                     profile={"species": "dog", "colour": "tan with white muzzle, chest and paws",
                              "markings": "curled tail", "ear_notch": False, "collar": "none", "sex_guess": "female",
                              "age_guess": "adult", "condition": "healthy"}))
        moti_p = ph("dog_2.jpg")
        s.add(Animal(name="Moti", species="dog", photo_id=moti_p, lat=LALPUR[0] + 0.0012, lon=LALPUR[1] - 0.0009,
                     guardians=["demo:priya"], rabies_vaccinated_on="unknown",
                     profile={"species": "dog", "colour": "white and tan", "markings": "patches, curled tail",
                              "ear_notch": False, "collar": "none"}))
        s.add(Animal(name="Billu", species="cat", photo_id=ph("cat_0.jpg"), lat=LALPUR[0] - 0.0015, lon=LALPUR[1] + 0.0011,
                     guardians=["demo:neha"], profile={"species": "cat", "colour": "white and ginger", "markings": "ginger patches on head"}))

        # A cow someone photographed near Ratu Chowk yesterday: the try-it "lost cow" sample matches this.
        s.add(Sighting(user_id="demo:sunita", photo_id=ph("cow_7.jpg"), species="cow",
                       profile={"species": "cow", "colour": "white"}, lat=RATU_RD[0] + 0.012, lon=RATU_RD[1] + 0.008,
                       source="seen", created=now() - timedelta(hours=20)))
        for f, dlat in (("cow_3.jpg", 0.02), ("cow_5.jpg", -0.015), ("cow_6.jpg", 0.03)):
            s.add(Sighting(user_id="demo:sunita", photo_id=ph(f), species="cow", profile={"species": "cow"},
                           lat=RATU_RD[0] + dlat, lon=RATU_RD[1], source="seen", created=now() - timedelta(hours=30)))

        s.add(LostPost(code="LF-1001", owner_id="demo:ramesh", name="Gauri", species="cow", photo_id=ph("cow_4.jpg"),
                       profile={"species": "cow", "colour": "white", "markings": "curved horns", "ear_tag": True},
                       lat=RATU_RD[0], lon=RATU_RD[1], area_label="Ratu Road, Ranchi", last_seen_text="8 Nov, evening",
                       created=now() - timedelta(hours=26)))
        s.add(LostPost(code="LF-1002", owner_id="demo:neha", name="Kalu", species="dog", photo_id=ph("dog_7.jpg"),
                       profile={"species": "dog", "colour": "tan with white chin and paws", "markings": "long legs, light eyebrows"},
                       lat=LALPUR[0], lon=LALPUR[1], area_label="Lalpur, Ranchi", last_seen_text="7 Nov, morning",
                       created=now() - timedelta(days=2)))
        s.add(LostPost(code="LF-1003", owner_id="demo:ramesh", name="Lakshmi", species="cow", photo_id=ph("cow_5.jpg"),
                       profile={"species": "cow", "colour": "white", "markings": "long horns"}, status="found",
                       lat=RATU_RD[0], lon=RATU_RD[1], area_label="Hatia, Ranchi", last_seen_text="3 Nov",
                       created=now() - timedelta(days=6), found_at=now() - timedelta(days=5, hours=5)))
        s.commit()


if __name__ == "__main__":
    from .db import init_db
    init_db()
    seed()
    with SessionLocal() as s:
        print("animals:", len(list(s.scalars(select(Animal)))))
