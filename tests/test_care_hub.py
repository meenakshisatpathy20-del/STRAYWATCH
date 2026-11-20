"""Vet hub: appointment slots and booking, and the food shop (server-side pricing, validation, orders)."""
from fastapi.testclient import TestClient

from app import care
from app.db import Base, Booking, Order, SessionLocal, VetProfile, engine as db_engine
from app.main import app


def client():
    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with SessionLocal() as s:
        s.add(VetProfile(code="VET-T1", name="Dr. Real", council="x", reg_no="1", verified=True, online=False, fee=199,
                         langs=["en"], species=["any"], user_id=None))
        s.add(VetProfile(code="VET-T2", name="Dr. Unverified", council="x", reg_no="2", verified=False, langs=["en"]))
        s.commit()
    return TestClient(app)


def form(day, slot, **kw):
    f = {"vet": "1", "mode": "video", "day": day, "slot": slot, "who": "pet", "species": "dog", "pet_name": "Bruno",
         "problem": "Not eating for 2 days", "owner_name": "Asha", "phone": "+91 98765 43210", "consent": "yes"}
    f.update(kw)
    return f


def test_booking_takes_a_slot_once_and_can_be_cancelled_by_the_owner():
    c = client()
    day = care.open_days()[1][0]
    slot = c.get(f"/api/vet/slots?vet=1&day={day}").json()["slots"][0]
    r = c.post("/vet/book", data=form(day, slot), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/vet/booking/BK-")
    page = c.get(r.headers["location"])
    assert "Join the video call" in page.text and "98xxxxxx10" in page.text
    assert slot not in c.get(f"/api/vet/slots?vet=1&day={day}").json()["slots"]
    assert c.post("/vet/book", data=form(day, slot), follow_redirects=False).status_code == 409
    other = TestClient(app)  # someone else can't cancel or see the phone
    assert "98xxxxxx10" not in other.get(r.headers["location"]).text
    assert other.post(r.headers["location"] + "/cancel", follow_redirects=False).status_code == 404
    c.post(r.headers["location"] + "/cancel")
    with SessionLocal() as s:
        assert s.query(Booking).one().status == "cancelled"
    assert slot in c.get(f"/api/vet/slots?vet=1&day={day}").json()["slots"]


def test_booking_rejects_unverified_vets_bad_phone_and_closed_days():
    c = client()
    day = care.open_days()[1][0]
    assert c.post("/vet/book", data=form(day, "17:00", vet="2")).status_code == 404
    assert c.post("/vet/book", data=form(day, "17:00", phone="123")).status_code == 400
    assert c.post("/vet/book", data=form("2001-01-01", "17:00")).status_code == 404


def test_shop_prices_on_the_server_and_validates_the_order():
    c = client()
    ok = {"cart": {"p-puppy": 2, "p-bowl": 1, "fake": 5}, "name": "Asha", "phone": "9876543210",
          "address": "Lalpur, Ranchi", "pincode": "834001"}
    r = c.post("/api/shop/order", json=ok)
    assert r.status_code == 200
    with SessionLocal() as s:
        o = s.query(Order).one()
        assert o.total == 2 * 349 + 199 and [i["id"] for i in o.items] == ["p-puppy", "p-bowl"]
    assert c.post("/api/shop/order", json={**ok, "cart": {}}).status_code == 400
    assert c.post("/api/shop/order", json={**ok, "pincode": "12"}).status_code == 400
    small = c.post("/api/shop/order", json={**ok, "cart": {"p-comb": 1}}).json()
    with SessionLocal() as s:
        assert s.query(Order).filter(Order.code == small["code"]).one().total == 99 + care.DELIVERY_FEE


def test_feeding_pack_goes_to_a_feeder_without_a_home_address():
    c = client()
    r = c.post("/api/shop/order", json={"cart": {"p-feedpack": 1}, "name": "Asha", "phone": "9876543210",
                                        "address": "", "pincode": "834001"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.query(Order).one().for_strays


def test_vet_hub_pages_render():
    c = client()
    for url in ("/vet", "/vet/book", "/shop", "/shop?a=cow"):
        assert c.get(url).status_code == 200, url
