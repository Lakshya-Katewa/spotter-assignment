"""Driving route from the public OSRM server: at most one HTTP call per route.

The call asks for the full geometry (encoded polyline) and the route steps; the
steps tell us which Interstate/US highways the route uses, and where.

* Concurrent requests for the same uncached route share one upstream call
  (see singleflight). Failures are cached briefly so an outage doesn't cause a
  stampede: 30 s for upstream errors, 1 h for "no route" (deterministic).
* Every field of the upstream response is validated; anything malformed becomes
  a controlled RoutingError (502) instead of an unhandled exception.
"""

import math
from dataclasses import dataclass, replace

import requests
from django.conf import settings

from fuel.services import singleflight
from fuel.services.highways import spans_from_steps
from fuel.services.polyline import decode

METERS_PER_MILE = 1609.344


class RoutingError(RuntimeError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status  # 502: routing service failed/malformed; 422: no route; 503: busy


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_seconds: float
    coordinates: list  # [[lon, lat], ...] (GeoJSON order)
    highways: list = None  # [(start_mile, end_mile, frozenset of refs)] from the route steps
    from_cache: bool = False


@dataclass(frozen=True)
class _Failure:
    message: str
    status: int


def _ttl(value):
    if isinstance(value, _Failure):
        return 3600 if value.status == 422 else 30
    return settings.ROUTE_CACHE_SECONDS


def get_route(start_lat, start_lon, end_lat, end_lon) -> Route:
    key = f"route:{start_lat:.5f},{start_lon:.5f}:{end_lat:.5f},{end_lon:.5f}"
    try:
        result, cached = singleflight.get_or_compute(
            key,
            lambda: _fetch(start_lat, start_lon, end_lat, end_lon),
            ttl=_ttl,
            lock_seconds=settings.EXTERNAL_TIMEOUT + 5,
            wait_seconds=settings.EXTERNAL_TIMEOUT + 5,
        )
    except singleflight.Busy:
        raise RoutingError("Routing is busy; please retry.", status=503) from None
    if isinstance(result, _Failure):
        raise RoutingError(result.message, result.status)
    return replace(result, from_cache=True) if cached else result


def _fetch(start_lat, start_lon, end_lat, end_lon):
    """One HTTP call. Returns a Route or a _Failure (never raises for upstream problems)."""
    url = f"{settings.OSRM_URL}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    try:
        r = requests.get(
            url,
            params={"overview": "full", "geometries": "polyline", "alternatives": "false", "steps": "true"},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=settings.EXTERNAL_TIMEOUT,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        return _Failure(f"Routing service unavailable: {exc}", 502)
    try:
        return parse_osrm(r.status_code, data)
    except RoutingError as exc:
        return _Failure(str(exc), exc.status)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def parse_osrm(status_code, data) -> Route:
    code = data.get("code") if isinstance(data, dict) else None
    if code in ("NoRoute", "NoSegment"):
        raise RoutingError(f"No drivable route between these points ({code}).", status=422)
    if status_code != 200 or code != "Ok":
        raise RoutingError(f"Routing service error ({code or status_code}).")
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise RoutingError("Malformed routing response: no routes.")
    best = routes[0]

    for field in ("distance", "duration"):
        if not _is_number(best.get(field)):
            raise RoutingError(f"Malformed routing response: invalid {field}.")
    try:
        coords = decode(best.get("geometry"))
    except ValueError as exc:
        raise RoutingError(f"Malformed routing response: {exc}.") from exc
    if len(coords) < 2 and best["distance"] > 0:
        raise RoutingError("Malformed routing response: geometry has fewer than 2 points.")
    if not all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in coords):
        raise RoutingError("Malformed routing response: coordinates out of range.")

    # Steps only feed highway matching: skip malformed ones rather than fail the route.
    legs = best.get("legs") if isinstance(best.get("legs"), list) else []
    steps = [
        s
        for leg in legs
        if isinstance(leg, dict) and isinstance(leg.get("steps"), list)
        for s in leg["steps"]
        if isinstance(s, dict) and _is_number(s.get("distance")) and isinstance(s.get("ref", ""), str)
    ]
    return Route(
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=float(best["duration"]),
        coordinates=coords,
        highways=spans_from_steps(steps, METERS_PER_MILE),
    )
