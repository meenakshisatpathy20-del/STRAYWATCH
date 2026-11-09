"""Database models. SQLite locally, Postgres in production (DATABASE_URL)."""
from __future__ import annotations

import math
import secrets
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    """Anyone who talks to StrayWatch. id is 'wa:<phone>', 'web:<token>' or 'demo:<name>'."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(80))
    lang: Mapped[str | None] = mapped_column(String(8))
    is_volunteer: Mapped[bool] = mapped_column(Boolean, default=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    trust: Mapped[int] = mapped_column(Integer, default=0)  # +1 confirmed useful report, -1 rejected
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: secrets.token_urlsafe(12))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Animal(Base):
    """A community animal registered by its caretaker(s)."""
    __tablename__ = "animals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    species: Mapped[str] = mapped_column(String(20))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # AI description: colours, marks, ear notch...
    photo_id: Mapped[str | None] = mapped_column(String(24))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    guardians: Mapped[list] = mapped_column(JSON, default=list)  # user ids
    rabies_vaccinated_on: Mapped[str | None] = mapped_column(String(20))  # YYYY-MM-DD, 'unknown' or None
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | deceased
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class CheckIn(Base):
    __tablename__ = "checkins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    animal_id: Mapped[int] = mapped_column(ForeignKey("animals.id"))
    user_id: Mapped[str] = mapped_column(String(80))
    photo_id: Mapped[str | None] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    concern: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Case(Base):
    """A rescue SOS."""
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reporter_id: Mapped[str] = mapped_column(String(80))
    photo_id: Mapped[str | None] = mapped_column(String(24))
    triage: Mapped[dict] = mapped_column(JSON, default=dict)
    animal_id: Mapped[int | None] = mapped_column(Integer)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    queue: Mapped[list] = mapped_column(JSON, default=list)      # helper user ids still to ask
    asked: Mapped[list] = mapped_column(JSON, default=list)      # helper user ids already asked
    current_helper: Mapped[str | None] = mapped_column(String(80))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime)
    assigned_to: Mapped[str | None] = mapped_column(String(80))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="alerting")  # alerting|assigned|closed|unanswered|rejected
    merged_reports: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class LostPost(Base):
    __tablename__ = "lost_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)
    owner_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(60))
    species: Mapped[str] = mapped_column(String(20))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    photo_id: Mapped[str | None] = mapped_column(String(24))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    area_label: Mapped[str | None] = mapped_column(String(80))
    last_seen_text: Mapped[str | None] = mapped_column(String(120))
    public: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="lost")  # lost | found | closed
    rejected_sightings: Mapped[list] = mapped_column(JSON, default=list)
    found_at: Mapped[datetime | None] = mapped_column(DateTime)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Sighting(Base):
    """Any photo of an animal with a location: SOS photos, check-ins, 'I saw this' reports."""
    __tablename__ = "sightings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80))
    photo_id: Mapped[str] = mapped_column(String(24))
    species: Mapped[str | None] = mapped_column(String(20))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # sos | checkin | seen | register
    lost_code: Mapped[str | None] = mapped_column(String(12))
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Outbox(Base):
    """Messages for users who are not reachable on WhatsApp (browser and demo users)."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class Trace(Base):
    """Every AI and decision step, so people (and judges) can see how StrayWatch decided."""
    __tablename__ = "trace"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    step: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(40))
    ms: Mapped[int | None] = mapped_column(Integer)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)


class TranslationCache(Base):
    __tablename__ = "translations"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)


_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def km(lat1, lon1, lat2, lon2) -> float:
    """Distance in km between two points (haversine)."""
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_or_create_user(s: Session, user_id: str) -> User:
    u = s.get(User, user_id)
    if u is None:
        u = User(id=user_id, state={})
        s.add(u)
        s.flush()
    u.last_seen = now()
    return u


def nearby(s: Session, model, lat: float, lon: float, radius_km: float, **filters):
    """Rows of `model` within radius_km, nearest first, as (distance_km, row). A box pre-filter keeps it cheap."""
    dlat = radius_km / 111.0
    dlon = radius_km / max(1e-6, 111.0 * math.cos(math.radians(lat)))
    q = select(model).where(model.lat.between(lat - dlat, lat + dlat), model.lon.between(lon - dlon, lon + dlon))
    for k, v in filters.items():
        q = q.where(getattr(model, k) == v)
    rows = [(km(lat, lon, r.lat, r.lon), r) for r in s.scalars(q)]
    return sorted([x for x in rows if x[0] <= radius_km], key=lambda x: x[0])
