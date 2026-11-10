"""Resolve user-supplied locations to coordinates.

Accepted inputs, tried in order:
  1. "lat,lon"                    -> used as-is (no lookup)
  2. exactly "City, ST" / "City, State" (optionally ", USA")
                                  -> local Census place table (no network)
  3. anything else (addresses)    -> Nominatim, restricted to the US. Results are
     cached, and calls are rate-limited to 1/second (Nominatim usage policy).

Every result must fall inside the US national boundary (checked offline).
"""

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from fuel.models import Place
from fuel.services import boundary
from fuel.services.names import CANADIAN_PROVINCES, place_key

EARTH_RADIUS_MI = 3958.7613

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = set(STATE_NAMES.values())

_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class LocationError(ValueError):
    status = 400


class GeocoderUnavailable(LocationError):
    """The external geocoder failed or the rate limit could not be honoured in time."""

    status = 503


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "local" | "nominatim" | "nominatim-cache"

    def as_dict(self):
        return {"query_label": self.label, "lat": round(self.lat, 6), "lon": round(self.lon, 6), "resolved_by": self.source}


def in_usa(lat: float, lon: float) -> bool:
    return boundary.contains(lat, lon)


def haversine_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def resolve(query: str) -> Location:
    query = (query or "").strip()
    if not query:
        raise LocationError("Location is required.")

    tail = query.rsplit(",", 1)[-1].strip().upper()
    if "," in query and (tail in CANADIAN_PROVINCES or tail in {"CANADA", "MEXICO"}):
        raise LocationError(f"'{query}' is outside the USA.")

    loc = _from_coordinates(query) or _from_local_places(query) or _from_nominatim(query)
    if not in_usa(loc.lat, loc.lon):
        raise LocationError(f"'{query}' is outside the USA.")
    return loc


def _from_coordinates(query):
    m = _LATLON.match(query)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise LocationError(f"Invalid coordinates: '{query}'.")
    return Location(lat, lon, query, "coordinates")


_COUNTRY = {"USA", "US", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA"}


def _from_local_places(query):
    """Only plain "City, ST" input. Anything with more detail (a street, a
    building) goes to the geocoder rather than being collapsed to a city centre."""
    parts = [p.strip() for p in query.split(",")]
    if len(parts) == 3 and parts[2].upper() in _COUNTRY:
        parts = parts[:2]
    if len(parts) != 2:
        return None
    city, state = parts
    state = re.sub(r"\s+\d{5}(-\d{4})?$", "", state.strip())  # "TX 75201"
    state = STATE_NAMES.get(state.lower(), state.upper())
    if state not in STATE_CODES or not city or any(ch.isdigit() for ch in city):
        return None
    place = Place.objects.filter(key=place_key(city, state)).order_by("rank").first()
    if not place:
        return None
    return Location(place.lat, place.lon, f"{city}, {state}", "local")


# -- Nominatim -----------------------------------------------------------------
_rate_lock = threading.Lock()
_RATE_KEY = "nominatim:slot"


def _acquire_rate_slot():
    """Wait for permission to make one Nominatim request (max 1 per second).

    `cache.add` is atomic, so with a shared cache (Redis/Memcached) the limit
    holds across all worker processes; with the default in-process cache it
    holds per process. Gives up after GEOCODE_MAX_WAIT_SECONDS.
    """
    deadline = time.monotonic() + settings.GEOCODE_MAX_WAIT_SECONDS
    with _rate_lock:
        while not cache.add(_RATE_KEY, 1, timeout=1):
            if time.monotonic() >= deadline:
                raise GeocoderUnavailable("Geocoder is busy; please retry in a moment.")
            time.sleep(0.05)


def _cache_key(query):
    normalized = re.sub(r"\s+", " ", query.lower()).strip()
    return "geocode:" + hashlib.sha1(normalized.encode()).hexdigest()  # safe for any cache backend


def _from_nominatim(query):
    key = _cache_key(query)
    hit = cache.get(key)
    if hit is not None:
        if not hit:
            raise LocationError(f"Could not find '{query}' in the USA.")
        return Location(hit["lat"], hit["lon"], hit["label"], "nominatim-cache")

    _acquire_rate_slot()
    try:
        r = requests.get(
            settings.NOMINATIM_URL,
            params={"q": query, "countrycodes": "us", "format": "json", "limit": 1},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=settings.EXTERNAL_TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocoderUnavailable(f"Could not geocode '{query}': {exc}") from exc
    if not hits:
        cache.set(key, {}, settings.GEOCODE_NEGATIVE_CACHE_SECONDS)
        raise LocationError(f"Could not find '{query}' in the USA.")
    best = {"lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]), "label": hits[0].get("display_name", query)}
    cache.set(key, best, settings.GEOCODE_CACHE_SECONDS)
    return Location(best["lat"], best["lon"], best["label"], "nominatim")
