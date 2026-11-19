"""Demo data so the website can be tried without real people.

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
from .db import Animal, Case, CaseEvent, LostPost, SessionLocal, Setting, Sighting, User, VetProfile, now

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


# Sample responder teams ("caretaker first, then the nearest right team"). All clearly marked "Sample".
RESPONDERS = [
    ("demo:ngo", "Sample NGO rescue team", "ngo", "en", (23.3690, 85.3250)),
    ("demo:vet", "Sample partner vet clinic", "vet", "hi", (23.3600, 85.3350)),
    ("demo:cattle", "Sample municipal cattle team", "municipal_cattle", "hi", (23.3800, 85.3100)),
    ("demo:abc", "Sample municipal animal team", "municipal", "hi", (23.3550, 85.3200)),
    ("demo:police", "Sample traffic police desk", "police", "hi", (23.3650, 85.3300)),
    ("demo:coord", "Sample duty coordinator", "coordinator", "en", (23.3441, 85.3096)),
]


# Sample online vets for the demo. Clearly labelled "Sample"; real vets sign up at /vet/join and are verified first.
SAMPLE_VETS = [
    ("demo:vet1", "VET-1001", "Dr. Sample Asha (sample vet)", "Sample Pet Clinic, Lalpur", ["en", "hi"], 199, True, True),
    ("demo:vet2", "VET-1002", "Dr. Sample Rohan (sample vet)", "Sample Animal Hospital, Ratu Road", ["en", "hi", "bn"], 0, True, True),
    ("demo:vet3", "VET-1003", "Dr. Sample Meena (sample vet)", "Sample Cattle Care, Kanke", ["hi", "en"], 149, True, False),
]


def seed_vets(s):
    for uid, code, name, clinic, langs, fee, strays, online in SAMPLE_VETS:
        if not s.get(User, uid):
            s.add(User(id=uid, name=name, lang="en", role="vet", state={}))
        if not s.query(VetProfile).filter_by(code=code).first():
            s.add(VetProfile(user_id=uid, code=code, name=name, clinic=clinic, qualification="B.V.Sc. & A.H. (sample)",
                             council="Sample State Veterinary Council", reg_no="SAMPLE", verified=True, online=online,
                             langs=langs, species=["any"], fee=fee, free_for_strays=strays, sample=True))
    s.commit()


def seed_responders(s):
    for uid, org, role, lang, (lat, lon) in RESPONDERS:
        u = s.get(User, uid)
        if not u:
            s.add(User(id=uid, name=org, org=org, role=role, lang=lang, is_volunteer=False, lat=lat, lon=lon, state={}))
        else:  # keep sample teams in step with the current rules
            u.name, u.org, u.role = org, org, role
    s.commit()


SAMPLE_VERSION = "2"  # bump when the sample cases change


def seed_cases(s):
    """Three finished SAMPLE cases with full ledgers, so a fresh demo isn't empty. Clearly labelled as samples."""
    ver = s.get(Setting, "sample_cases")
    if ver and ver.value == SAMPLE_VERSION:
        return
    # Replace older sample cases (only ones reported by sample users) with the current set.
    old = list(s.query(Case).filter(Case.reporter_id.like("demo:%")))
    for c in old:
        s.query(CaseEvent).filter(CaseEvent.case_id == c.id).delete()
        s.delete(c)
    s.merge(Setting(key="sample_cases", value=SAMPLE_VERSION))

    def ph(fname):
        return photos.save(s, _jpg(PHOTOS / fname)).id

    def case(created_h_ago, events, **kw):
        c = Case(created=now() - timedelta(hours=created_h_ago), track_key="sample", **kw)
        s.add(c)
        s.flush()
        for mins, agent, kind, detail in events:
            s.add(CaseEvent(case_id=c.id, agent=agent, kind=kind, detail=detail,
                            created=c.created + timedelta(minutes=mins)))
        return c

    t0 = 30
    case(t0, [
        (0, "Person", "reported", "Sample case: a citizen reported a dog with a photo and the words “gaadi ne maara”."),
        (0.05, "Triage", "triaged", "Adult tan dog lying by the road, holding up its front leg. (condition: injured, on road: yes)"),
        (0.06, "Rules", "ruled", "HIGH: hurt animal on the road: it needs care, and it can cause an accident"),
        (0.08, "Dispatch", "planned", "Order: animal rescue NGO → partner vet → StrayWatch volunteer → duty coordinator. Each gets 3 minutes."),
        (0.1, "Dispatch", "alerted", "Alerted animal rescue NGO (responder #1, 0.9 km away). Waiting 3 minutes."),
        (2.4, "Person", "accepted", "Sample NGO rescue team accepted after 2.4 min."),
        (41, "Dispatch", "closed", "Closed after 41 min. Proof photo checked by AI: dog resting on a blanket at a clinic.")],
        reporter_id="demo:neha", photo_id=ph("dog_5.jpg"), lat=23.3690, lon=85.3310, status="closed", level="HIGH",
        reasons=["hurt animal on the road: it needs care, and it can cause an accident"], teams=["ngo", "vet", "volunteer"], hop_minutes=3, source="citizen",
        assigned_to="demo:ngo", accepted_at=now() - timedelta(hours=t0) + timedelta(minutes=2.4),
        closed_at=now() - timedelta(hours=t0) + timedelta(minutes=41), proof_photo_id=ph("dog_3.jpg"),
        proof_note="Sample: dog resting on a blanket at a clinic.",
        triage={"species": "dog", "condition": "injured", "on_road": True, "summary": "Sample: adult tan dog lying by the road, holding up its front leg."})
    t1 = 7
    case(t1, [
        (0, "Camera", "reported", "Sample camera · Ratu Road junction: a cow has been lying in a traffic lane for 34 s."),
        (0.05, "Triage", "triaged", "A cow lying in the left lane, unable to get up; a wound on its hind leg. (condition: injured, on road: yes)"),
        (0.06, "Rules", "ruled", "HIGH: hurt animal on the road: it needs care, and it can cause an accident"),
        (0.08, "Dispatch", "planned", "Order: animal rescue NGO → partner vet → municipal cattle team → traffic police → StrayWatch volunteer → duty coordinator. Each gets 3 minutes."),
        (0.1, "Dispatch", "alerted", "Alerted animal rescue NGO (responder #1, 2.4 km away). Waiting 3 minutes."),
        (2.1, "Person", "accepted", "Sample NGO rescue team accepted after 2.1 min."),
        (2.3, "Dispatch", "alerted", "Alerted traffic police (extra help: guard the spot). Waiting 3 minutes."),
        (38, "Dispatch", "closed", "Closed after 38 min. Proof photo checked by AI: cow on the roadside verge being treated.")],
        reporter_id="demo:camera", photo_id=ph("cow_3.jpg"), lat=23.3906, lon=85.3051, status="closed", level="HIGH",
        reasons=["hurt animal on the road: it needs care, and it can cause an accident"],
        teams=["ngo", "vet", "municipal_cattle", "police", "volunteer"], hop_minutes=3,
        source="camera", assigned_to="demo:ngo", accepted_at=now() - timedelta(hours=t1) + timedelta(minutes=2.1),
        closed_at=now() - timedelta(hours=t1) + timedelta(minutes=38), proof_photo_id=ph("cow_6.jpg"),
        proof_note="Sample: cow on the roadside verge being treated.",
        triage={"species": "cow", "condition": "injured", "on_road": True, "blocking_lane": True, "lying_still": True,
                "summary": "Sample: a cow lying in the left lane with a hind-leg wound."})
    # Same road, same time of day, the day before: the Hotspot agent should notice the pattern.
    case(t1 + 24, [
        (0, "Person", "reported", "Sample case: a citizen reported a dog hit by a bike on Ratu Road."),
        (0.05, "Triage", "triaged", "A dog lying at the edge of the road, bleeding from a front leg. (condition: injured, on road: yes)"),
        (0.06, "Rules", "ruled", "HIGH: hurt animal on the road: it needs care, and it can cause an accident"),
        (0.1, "Dispatch", "alerted", "Alerted animal rescue NGO (responder #1, 2.5 km away). Waiting 3 minutes."),
        (1.6, "Person", "accepted", "Sample NGO rescue team accepted after 1.6 min."),
        (45, "Dispatch", "closed", "Closed after 45 min. Proof photo checked by AI: dog bandaged at a clinic.")],
        reporter_id="demo:sunita", photo_id=ph("dog_7.jpg"), lat=23.3912, lon=85.3046, status="closed", level="HIGH",
        reasons=["hurt animal on the road: it needs care, and it can cause an accident"], teams=["ngo", "vet", "volunteer"],
        hop_minutes=3, source="citizen", assigned_to="demo:ngo",
        accepted_at=now() - timedelta(hours=t1 + 24) + timedelta(minutes=1.6),
        closed_at=now() - timedelta(hours=t1 + 24) + timedelta(minutes=45), proof_photo_id=ph("dog_3.jpg"),
        proof_note="Sample: dog bandaged at a clinic.",
        triage={"species": "dog", "condition": "injured", "on_road": True, "summary": "Sample: a dog hit by a bike, bleeding from a front leg."})
    case(3, [
        (0, "Person", "reported", "Sample case: a citizen reported a cat with a photo."),
        (0.05, "Triage", "triaged", "Adult cat resting on a wall, no visible injuries. (condition: healthy, on road: no)"),
        (0.06, "Rules", "ruled", "LOW: healthy animal: no rescue needed"),
        (0.07, "Hotspot", "logged", "Added to the hotspot map. No responder disturbed.")],
        reporter_id="demo:sunita", photo_id=ph("cat_2.jpg"), lat=23.3550, lon=85.3150, status="logged", level="LOW",
        reasons=["healthy animal: no rescue needed"], teams=[], hop_minutes=0, source="citizen",
        triage={"species": "cat", "condition": "healthy", "on_road": False, "summary": "Sample: adult cat resting on a wall."})
    s.commit()


def seed():
    make_samples()
    with SessionLocal() as s:
        seed_responders(s)
        seed_vets(s)
        seed_cases(s)
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
        # Sample: cattle repeatedly reported on Ratu Road, so the City view shows a hotspot.
        for f, dlat, hrs in (("cow_3.jpg", 0.001, 30), ("cow_5.jpg", -0.001, 54), ("cow_6.jpg", 0.002, 78)):
            s.add(Sighting(user_id="demo:sunita", photo_id=ph(f), species="cow", profile={"species": "cow", "on_road": True},
                           lat=RATU_RD[0] + dlat, lon=RATU_RD[1] + 0.001, source="seen", created=now() - timedelta(hours=hrs)))

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
