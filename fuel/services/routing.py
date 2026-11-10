"""Driving route from the public OSRM server: exactly one HTTP call per route."""

from dataclasses import dataclass, replace

import requests
from django.conf import settings
from django.core.cache import cache

from fuel.services.polyline import decode

METERS_PER_MILE = 1609.344


class RoutingError(RuntimeError):
    pass


@dataclass
class Route:
    distance_miles: float
    duration_seconds: float
    coordinates: list  # [[lon, lat], ...] (GeoJSON order)
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
            params={"overview": "full", "geometries": "polyline", "alternatives": "false", "steps": "false"},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=settings.EXTERNAL_TIMEOUT,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    if r.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"No route found ({data.get('code', r.status_code)}: {data.get('message', '')})".strip())

    best = data["routes"][0]
    route = Route(
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=best["duration"],
        coordinates=decode(best["geometry"]),
    )
    cache.set(key, route, settings.ROUTE_CACHE_SECONDS)
    return route
