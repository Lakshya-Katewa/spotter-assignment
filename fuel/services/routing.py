"""Driving route from the public OSRM server: at most one HTTP call per route.

The call asks for the route steps, without the separate overview line: the steps
tell us which Interstate/US highways the route uses, and where, and their
geometries joined end to end are exactly the full route line (checked against
`overview=full` on real routes). Not downloading the line twice cuts the response
from ~218 KB to ~134 KB on the wire for a cross-country route, and the median call
time by ~0.25 s (measured on the public server).

* Concurrent requests for the same uncached route share one upstream call
  (see singleflight). Failures are cached briefly so an outage doesn't cause a
  stampede: 30 s for upstream errors, 1 h for "no route" (deterministic).
* Calls are rate-limited to 1/second, per the demo server's usage policy.
* The cache stores the steps' encoded polylines (~120 KB for a cross-country route)
  rather than decoded coordinates (~780 KB pickled); decoding takes ~10 ms.
* Every field of the upstream response is validated; anything malformed becomes
  a controlled RoutingError (502) instead of an unhandled exception.
"""

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass

import requests
from django.conf import settings

from fuel.services import ratelimit, singleflight
from fuel.services.highways import spans_from_steps
from fuel.services.polyline import decode

METERS_PER_MILE = 1609.344
logger = logging.getLogger(__name__)
# One persistent HTTP session: the TLS connection is reused between calls (kept open
# by the server for over a minute), which saves ~0.35 s of handshake per call.
# urllib3's connection pool is thread-safe for these plain GETs.
_session = requests.Session()


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
class _Compact:
    """What the cache holds: the route with its geometry still encoded (one polyline per step)."""

    distance_miles: float
    duration_seconds: float
    polylines: tuple
    highways: list


@dataclass(frozen=True)
class _Failure:
    message: str
    status: int


def _ttl(value):
    if isinstance(value, _Failure):
        return 3600 if value.status == 422 else 30
    return settings.ROUTE_CACHE_SECONDS


def warm_up_connection():
    """Open the connection to the routing server in a background thread.

    Sends one `HEAD /`: not a routing request (OSRM answers it at once without computing
    anything), so it doesn't count against the 1 request/second policy. The persistent
    session then reuses this connection for the first real call. Failures are ignored:
    the first request simply opens its own connection. Off with OSRM_WARMUP=0.
    """
    if not settings.OSRM_WARMUP:
        return

    def run():
        try:
            _session.head(f"{settings.OSRM_URL}/", headers={"User-Agent": settings.HTTP_USER_AGENT},
                          timeout=settings.EXTERNAL_TIMEOUT)
        except requests.RequestException as exc:
            logger.info("Routing connection warm-up failed (harmless): %s", exc)

    threading.Thread(target=run, name="osrm-warmup", daemon=True).start()


def cache_key(start_lat, start_lon, end_lat, end_lon) -> str:
    """Namespaced by routing provider, so switching OSRM_URL never serves another server's routes."""
    provider = hashlib.sha1(settings.OSRM_URL.encode()).hexdigest()[:10]
    return f"route:{provider}:{start_lat:.5f},{start_lon:.5f}:{end_lat:.5f},{end_lon:.5f}"


def get_route(start_lat, start_lon, end_lat, end_lon) -> Route:
    key = cache_key(start_lat, start_lon, end_lat, end_lon)
    try:
        result, cached = singleflight.get_or_compute(
            key,
            lambda: _fetch(start_lat, start_lon, end_lat, end_lon),
            ttl=_ttl,
            # Leader's worst case: rate-limit wait + connect timeout + read timeout.
            lock_seconds=settings.ROUTING_MAX_WAIT_SECONDS + 2 * settings.EXTERNAL_TIMEOUT + 5,
            wait_seconds=settings.ROUTING_MAX_WAIT_SECONDS + 2 * settings.EXTERNAL_TIMEOUT + 5,
        )
    except singleflight.Busy:
        raise RoutingError("Routing is busy; please retry.", status=503) from None
    if isinstance(result, _Failure):
        raise RoutingError(result.message, result.status)
    return Route(
        distance_miles=result.distance_miles,
        duration_seconds=result.duration_seconds,
        coordinates=join_steps(result.polylines),  # validated before caching
        highways=result.highways,
        from_cache=cached,
    )


def _fetch(start_lat, start_lon, end_lat, end_lon):
    """One HTTP call. Returns a _Compact or a _Failure (cached briefly).

    Raises RoutingError(503) without caching if the rate limit can't be met in time.
    """
    url = f"{settings.OSRM_URL}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    try:
        ratelimit.acquire("osrm", time.monotonic() + settings.ROUTING_MAX_WAIT_SECONDS)
    except ratelimit.RateLimited:
        raise RoutingError("Routing is busy; please retry in a moment.", status=503) from None
    try:
        r = _session.get(
            url,
            params={"overview": "false", "geometries": "polyline", "alternatives": "false", "steps": "true"},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=settings.EXTERNAL_TIMEOUT,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OSRM request failed: %s", exc)
        return _Failure("Routing service unavailable; please retry later.", 502)  # details are logged, not returned
    try:
        route = parse_osrm(r.status_code, data)
    except RoutingError as exc:
        if exc.status != 422:
            logger.warning("OSRM error (HTTP %s): %s", r.status_code, exc)
        return _Failure(str(exc), exc.status)
    return _Compact(route.distance_miles, route.duration_seconds, step_polylines(data["routes"][0]), route.highways)


def step_polylines(route) -> tuple:
    """The encoded geometry of every step, in order. Raises RoutingError if any is missing."""
    legs = route.get("legs")
    if not isinstance(legs, list) or not legs:
        raise RoutingError("Malformed routing response: no route legs.")
    out = []
    for leg in legs:
        steps = leg.get("steps") if isinstance(leg, dict) else None
        if not isinstance(steps, list) or not steps:
            raise RoutingError("Malformed routing response: a leg has no steps.")
        for step in steps:
            geometry = step.get("geometry") if isinstance(step, dict) else None
            if not isinstance(geometry, str):
                raise RoutingError("Malformed routing response: a step has no geometry.")
            out.append(geometry)
    return tuple(out)


def join_steps(polylines) -> list:
    """The route line: step geometries end to end, each shared joint point kept once."""
    coords = []
    for encoded in polylines:
        part = decode(encoded)
        coords.extend(part[1:] if coords and part and part[0] == coords[-1] else part)
    return coords


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
        coords = join_steps(step_polylines(best))
    except ValueError as exc:  # a bad polyline (a missing one already raised RoutingError)
        raise RoutingError(f"Malformed routing response: {exc}.") from exc
    if len(coords) < 2 and best["distance"] > 0:
        raise RoutingError("Malformed routing response: geometry has fewer than 2 points.")
    if not all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in coords):
        raise RoutingError("Malformed routing response: coordinates out of range.")

    # A step's distance and ref only feed highway matching: a malformed one is left out
    # of that, rather than failing the route (its geometry was validated above).
    steps = [
        s
        for leg in best["legs"]
        for s in leg["steps"]
        if _is_number(s.get("distance")) and isinstance(s.get("ref", ""), str)
    ]
    return Route(
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=float(best["duration"]),
        coordinates=coords,
        highways=spans_from_steps(steps, METERS_PER_MILE),
    )
