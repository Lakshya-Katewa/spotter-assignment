"""Driving route from the public OSRM server: exactly one HTTP call per route.

The call asks for the full geometry (encoded polyline) and the route steps; the
steps tell us which Interstate/US highways the route uses, and where.
"""

from dataclasses import dataclass, replace

import requests
from django.conf import settings
from django.core.cache import cache

from fuel.services.highways import spans_from_steps
from fuel.services.polyline import decode

METERS_PER_MILE = 1609.344


class RoutingError(RuntimeError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status  # 502: routing service failed; 422: no route between the points


@dataclass
class Route:
    distance_miles: float
    duration_seconds: float
    coordinates: list  # [[lon, lat], ...] (GeoJSON order)
    highways: list = None  # [(start_mile, end_mile, frozenset of refs)] from the route steps
    from_cache: bool = False


def get_route(start_lat, start_lon, end_lat, end_lon) -> Route:
    key = f"route:{start_lat:.5f},{start_lon:.5f}:{end_lat:.5f},{end_lon:.5f}"
    cached = cache.get(key)
    if cached:
        return replace(cached, from_cache=True)

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
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    code = data.get("code") if isinstance(data, dict) else None
    if code in ("NoRoute", "NoSegment"):
        raise RoutingError(f"No drivable route between these points ({code}).", status=422)
    if r.status_code != 200 or code != "Ok" or not data.get("routes"):
        raise RoutingError(f"Routing service error ({code or r.status_code}).")

    best = data["routes"][0]
    route = Route(
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=best["duration"],
        coordinates=decode(best["geometry"]),
        highways=spans_from_steps((s for leg in best.get("legs", []) for s in leg.get("steps", [])), METERS_PER_MILE),
    )
    cache.set(key, route, settings.ROUTE_CACHE_SECONDS)
    return route
