"""Resolve user-supplied locations to coordinates.

Accepted inputs, tried in order:
  1. "lat,lon"                    -> used as-is (no lookup)
  2. "City, ST" / "City, State"   -> local Census place table (no network)
  3. anything else (addresses)    -> one Nominatim call, restricted to the US
"""

import math
import re
from dataclasses import dataclass

import requests
from django.conf import settings

from fuel.models import Place
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

# Rough bounding boxes: contiguous US, Alaska, Hawaii.
_US_BOXES = [(24.3, 49.5, -125.0, -66.8), (51.0, 71.6, -180.0, -129.9), (18.8, 22.4, -160.5, -154.7)]
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class LocationError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "local" | "nominatim"

    def as_dict(self):
        return {"query_label": self.label, "lat": round(self.lat, 6), "lon": round(self.lon, 6), "resolved_by": self.source}


def in_usa(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in _US_BOXES)


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


def _from_local_places(query):
    parts = [p.strip() for p in query.split(",")]
    if len(parts) < 2:
        return None
    city, state = parts[-2], parts[-1].upper()
    state = STATE_NAMES.get(state.lower(), state)
    state = re.sub(r"\s+\d{5}(-\d{4})?$", "", state)  # "TX 75201"
    if state == "USA" and len(parts) >= 3:
        city, state = parts[-3], STATE_NAMES.get(parts[-2].lower(), parts[-2].upper())
    if state not in STATE_CODES:
        return None
    place = Place.objects.filter(key=place_key(city, state)).order_by("rank").first()
    if not place:
        return None
    return Location(place.lat, place.lon, f"{city}, {state}", "local")


def _from_nominatim(query):
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
        raise LocationError(f"Could not geocode '{query}': {exc}") from exc
    if not hits:
        raise LocationError(f"Could not find '{query}' in the USA.")
    hit = hits[0]
    return Location(float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query), "nominatim")
