"""Turn a typed landmark ("Lalpur Chowk, Ranchi") into coordinates, for people who can't or won't share GPS.

Uses OpenStreetMap Nominatim (free; 1 request/second, identify the app). Results are cached. Never guesses:
if nothing is found, the caller asks again.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("straywatch.geo")
_cache: dict[str, dict | None] = {}


def geocode(text: str, near: tuple[float, float] | None = None) -> dict | None:
    """Returns {"lat", "lon", "label"} or None. Biased towards `near` (or the pilot city) but not limited to it."""
    q = " ".join(text.split())[:120]
    if len(q) < 3:
        return None
    lat, lon = near or config.CITY_CENTER
    key = f"{q.lower()}|{round(lat, 1)}|{round(lon, 1)}"
    if key in _cache:
        return _cache[key]
    params = {"q": q, "format": "json", "limit": 1, "countrycodes": "in", "addressdetails": 0,
              "viewbox": f"{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}", "bounded": 0}
    url = config.GEOCODER_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StrayWatch/1.0 (community animal rescue)"})
        with urllib.request.urlopen(req, timeout=6) as r:
            rows = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        log.warning("geocoding failed: %s", e)
        return None  # not cached: the service may be back next time
    hit = None
    if rows:
        r0 = rows[0]
        label = ", ".join(r0.get("display_name", q).split(", ")[:3])
        hit = {"lat": float(r0["lat"]), "lon": float(r0["lon"]), "label": label}
    _cache[key] = hit
    return hit
