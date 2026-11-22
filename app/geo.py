"""Turn a typed landmark ("Lalpur Chowk, Ranchi") into coordinates, for people who can't or won't share GPS.

Two free OpenStreetMap search services (Nominatim, then Photon as a fallback), a few simpler versions of the text
("Lalpur Chowk, Ranchi" → "Lalpur, Ranchi"), and a distance check so a same-named place far away is never used.
Results are cached. Never guesses: if nothing is found, the caller asks again, and the person always confirms.
"""
from __future__ import annotations

import json
import logging
import math
import re
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("straywatch.geo")
_cache: dict[str, dict | None] = {}
UA = {"User-Agent": "StrayWatch/1.0 (community animal rescue; https://straywatch-cacyarcncugkf3cr.indiasouthcentral-01.azurewebsites.net)"}
MAX_KM = 60  # a landmark further than this from the person (or the pilot city) is not the one they mean
_FILLER = r"(\b(chowk|chauk|chauraha|crossing|junction|near|ke paas|ke pass|paas|opposite|opp|samne|behind|piche|road side|corner)\b|चौक|चौराहा|के पास|के सामने)"


def _km(a, b, c, d) -> float:
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=6) as r:
        return json.loads(r.read())


def _nominatim(q: str, lat: float, lon: float) -> list[dict]:
    rows = _get(config.GEOCODER_URL + "?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 3, "countrycodes": "in", "addressdetails": 0,
         "viewbox": f"{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}", "bounded": 0}))
    return [{"lat": float(r["lat"]), "lon": float(r["lon"]), "label": ", ".join(r.get("display_name", q).split(", ")[:3])}
            for r in rows]


def _photon(q: str, lat: float, lon: float) -> list[dict]:
    d = _get(config.GEOCODER_FALLBACK_URL + "?" + urllib.parse.urlencode({"q": q, "limit": 3, "lat": lat, "lon": lon}))
    out = []
    for f in d.get("features", []):
        p, (x, y) = f.get("properties", {}), f["geometry"]["coordinates"]
        if p.get("countrycode", "IN").upper() != "IN":
            continue
        parts = [p.get("name"), p.get("street"), p.get("district") or p.get("locality"), p.get("city") or p.get("county")]
        out.append({"lat": float(y), "lon": float(x), "label": ", ".join(dict.fromkeys(x for x in parts if x))[:90] or q})
    return out


def variants(text: str) -> list[str]:
    """The text as typed, then simpler forms: filler words removed, then the last two comma parts."""
    q = " ".join(text.split())[:120]
    out = [q]
    simple = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", re.sub(_FILLER, " ", q, flags=re.I))).strip(" ,")
    if simple and simple.lower() != q.lower():
        out.append(simple)
    parts = [p.strip() for p in simple.split(",") if p.strip()]
    if len(parts) > 2:
        out.append(", ".join(parts[-2:]))
    return list(dict.fromkeys(out))


def geocode(text: str, near: tuple[float, float] | None = None) -> dict | None:
    """Returns {"lat", "lon", "label"} or None. Biased towards `near` (or the pilot city)."""
    if len((text or "").strip()) < 3:
        return None
    lat, lon = near or config.CITY_CENTER
    key = f"{text.strip().lower()}|{round(lat, 1)}|{round(lon, 1)}"
    if key in _cache:
        return _cache[key]
    failed = False
    for q in variants(text):
        for name, fn in (("nominatim", _nominatim), ("photon", _photon)):
            try:
                hits = fn(q, lat, lon)
            except Exception as e:  # noqa: BLE001 - try the next service
                log.warning("%s failed for %r: %s", name, q, e)
                failed = True
                continue
            hits = [h for h in hits if _km(lat, lon, h["lat"], h["lon"]) <= MAX_KM]
            if hits:
                _cache[key] = hits[0]
                return hits[0]
    if not failed:
        _cache[key] = None  # the services answered and found nothing; don't ask them again
    return None
