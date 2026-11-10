"""Route + fuel plan for a start/finish pair. One routing API call per uncached request."""

import copy
import hashlib
import math
import time

from django.conf import settings
from django.core.cache import cache

from fuel.models import FuelStation
from fuel.services import geo, routing
from fuel.services.polyline import simplify
from fuel.services.optimizer import NoFeasiblePlan, consolidate_stops, plan_fuel_stops
from fuel.services.stations import get_index


class PlanError(ValueError):
    """Bad input or an impossible trip; maps to HTTP 400/422."""

    status = 400


class UnroutableError(PlanError):
    status = 422


def build_plan(start: str, finish: str, include_geometry: bool = True, min_stop_savings: float | None = None) -> dict:
    if min_stop_savings is None:
        min_stop_savings = settings.MIN_STOP_SAVINGS
    if not math.isfinite(min_stop_savings) or min_stop_savings < 0:
        raise PlanError("min_stop_savings must be a number >= 0.")
    t0 = time.perf_counter()
    try:
        a, b = geo.resolve(start), geo.resolve(finish)
    except geo.LocationError as exc:
        raise PlanError(str(exc)) from exc

    cache_key = "plan:" + hashlib.sha1(f"{a.lat:.5f},{a.lon:.5f}|{b.lat:.5f},{b.lon:.5f}|{min_stop_savings:g}".encode()).hexdigest()
    result = cache.get(cache_key)
    cached = result is not None
    if not cached:
        result = _compute(a, b, min_stop_savings)
        cache.set(cache_key, result, settings.ROUTE_CACHE_SECONDS)

    out = dict(result)
    if not include_geometry:
        out["route"] = {k: v for k, v in out["route"].items() if k != "geometry"}
    out["meta"] = {
        **out["meta"],
        "cached": cached,
        "external_calls": {
            "routing": 0 if cached else out["meta"]["routing_calls"],
            "geocoding": sum(loc.source == "nominatim" for loc in (a, b)),
        },
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return out


def _compute(a, b, min_stop_savings) -> dict:
    try:
        route = routing.get_route(a.lat, a.lon, b.lat, b.lon)
    except routing.RoutingError as exc:
        raise UnroutableError(str(exc)) from exc

    max_range = settings.VEHICLE_MAX_RANGE_MILES
    mpg = settings.VEHICLE_MPG
    corridor = settings.STATION_CORRIDOR_MILES
    origin_window = settings.ORIGIN_STATION_WINDOW_MILES

    # Scale geometry miles to the router's official distance so mile markers line up.
    candidates, cum = get_index().along_route(route.coordinates, corridor)
    geom_miles = float(cum[-1]) if len(cum) else 0.0
    scale = route.distance_miles / geom_miles if geom_miles else 1.0
    for c in candidates:
        c.mile *= scale

    # Assumption: the tank starts (nearly) empty. The driver can reach a station within
    # `origin_window` miles of the start, and the fuel for those first miles is charged
    # at that first station's price, so total gallons = route miles / mpg.
    try:
        plan = plan_fuel_stops(
            ((c.station_id, c.mile, c.price) for c in candidates),
            total_miles=route.distance_miles,
            max_range_miles=max_range,
            mpg=mpg,
            origin_window_miles=origin_window,
        )
    except NoFeasiblePlan as exc:
        raise UnroutableError(str(exc)) from exc
    cheapest_cost = plan.total_cost
    cheapest_stops = len(plan.stops)
    plan = consolidate_stops(copy.deepcopy(plan), mpg, max_range / mpg, min_stop_savings)

    info = {s.opis_id: s for s in FuelStation.objects.filter(opis_id__in=[s.key for s in plan.stops])}
    offsets = {c.station_id: c.offset_miles for c in candidates}
    stops = []
    for n, s in enumerate(plan.stops, start=1):
        st = info[s.key]
        stops.append(
            {
                "stop": n,
                "opis_id": st.opis_id,
                "name": st.name,
                "address": st.address,
                "city": st.city,
                "state": st.state,
                "lat": round(st.lat, 6),
                "lon": round(st.lon, 6),
                "price_per_gallon": round(s.price, 4),
                "mile_marker": round(s.mile, 1),
                "distance_from_route_miles": round(offsets.get(s.key, 0.0), 1),
                "fuel_on_arrival_gallons": round(s.fuel_on_arrival_gal, 2),
                "gallons_purchased": round(s.gallons, 2),
                "cost": round(s.cost, 2),
            }
        )

    avg = plan.total_cost / plan.total_gallons if plan.total_gallons else 0.0
    return {
        "start": a.as_dict(),
        "finish": b.as_dict(),
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(x, 5), round(y, 5)] for x, y in simplify(route.coordinates)],
            },
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(plan.total_cost, 2),
            "total_gallons": round(plan.total_gallons, 2),
            "average_price_per_gallon": round(avg, 4),
            "number_of_stops": len(stops),
            "stations_considered": len(candidates),
            # The plan above skips stops that save less than `min_stop_savings` dollars.
            # These fields show what the strictly cheapest plan would cost.
            "cheapest_possible_cost": round(cheapest_cost, 2),
            "cheapest_possible_stops": cheapest_stops,
            "extra_cost_for_fewer_stops": round(plan.total_cost - cheapest_cost, 2),
        },
        "meta": {
            "assumptions": {
                "max_range_miles": max_range,
                "miles_per_gallon": mpg,
                "tank_gallons": max_range / mpg,
                "start_tank": "empty (first stop is within the first "
                f"{origin_window:g} miles; fuel for those miles is charged at that stop)",
                "station_corridor_miles": corridor,
                "station_location": "city centroid (the price file has no coordinates)",
            },
            "min_stop_savings": min_stop_savings,
            "routing_provider": settings.OSRM_URL,
            "routing_calls": 0 if route.from_cache else 1,
        },
    }
