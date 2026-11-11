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
from fuel.services import boundary, singleflight
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


class GeocoderBadResponse(GeocoderUnavailable):
    status = 502


def _acquire_rate_slot(deadline):
    """Reserve one Nominatim request slot (max 1 per second) before `deadline`.

    `cache.add` is atomic, so with a shared cache (Redis) the limit holds across
    all worker processes; with the default in-process cache it holds per process.
    The whole wait, including queueing behind other threads, is bounded.
    """
    if not _rate_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise GeocoderUnavailable("Geocoder is busy; please retry in a moment.")
    try:
        while True:
            if time.monotonic() >= deadline:
                raise GeocoderUnavailable("Geocoder is busy; please retry in a moment.")
            if cache.add(_RATE_KEY, 1, timeout=1):
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    finally:
        _rate_lock.release()


def _cache_key(query):
    normalized = re.sub(r"\s+", " ", query.lower()).strip()
    return "geocode:" + hashlib.sha1(normalized.encode()).hexdigest()  # safe for any cache backend


def parse_nominatim(hits, query):
    """{} for "no result", {lat, lon, label} for a hit; GeocoderBadResponse if malformed."""
    if not isinstance(hits, list):
        raise GeocoderBadResponse(f"Could not geocode '{query}': malformed geocoder response.")
    if not hits:
        return {}
    hit = hits[0]
    try:
        lat, lon = float(hit["lat"]), float(hit["lon"])
    except (TypeError, KeyError, ValueError):
        raise GeocoderBadResponse(f"Could not geocode '{query}': malformed geocoder response.") from None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeocoderBadResponse(f"Could not geocode '{query}': geocoder returned invalid coordinates.")
    label = hit.get("display_name")
    return {"lat": lat, "lon": lon, "label": label if isinstance(label, str) and label else query}


def _from_nominatim(query):
    key = _cache_key(query)
    deadline = time.monotonic() + settings.GEOCODE_MAX_WAIT_SECONDS

    def lookup():
        _acquire_rate_slot(deadline)
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
        return parse_nominatim(hits, query)

    # Identical concurrent lookups share one request; waiting re-checks the cache.
    try:
        best, cached = singleflight.get_or_compute(
            key,
            lookup,
            ttl=lambda v: settings.GEOCODE_CACHE_SECONDS if v else settings.GEOCODE_NEGATIVE_CACHE_SECONDS,
            lock_seconds=settings.EXTERNAL_TIMEOUT + settings.GEOCODE_MAX_WAIT_SECONDS,
            wait_seconds=settings.GEOCODE_MAX_WAIT_SECONDS + settings.EXTERNAL_TIMEOUT,
        )
    except singleflight.Busy:
        raise GeocoderUnavailable("Geocoder is busy; please retry in a moment.") from None
    if not best:
        raise LocationError(f"Could not find '{query}' in the USA.")
    return Location(best["lat"], best["lon"], best["label"], "nominatim-cache" if cached else "nominatim")
