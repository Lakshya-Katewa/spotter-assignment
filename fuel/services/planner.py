"""Route + fuel plan for a start/finish pair. One routing API call per uncached route."""

import copy
import hashlib
import math
import time

from django.conf import settings
from django.core.cache import cache

from fuel.services import geo, highways, routing
from fuel.services.optimizer import NoFeasiblePlan, consolidate_stops, plan_fuel_stops
from fuel.services.polyline import simplify
from fuel.services.stations import DatasetChanging, get_index


class PlanError(ValueError):
    """Bad input (400), an impossible trip (422) or an unavailable upstream (502/503)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def build_plan(
    start: str,
    finish: str,
    include_geometry: bool = True,
    min_stop_savings: float | None = None,
    start_fuel_gallons: float | None = None,
) -> dict:
    t0 = time.perf_counter()
    if min_stop_savings is None:
        min_stop_savings = settings.MIN_STOP_SAVINGS
    if not math.isfinite(min_stop_savings) or min_stop_savings < 0:
        raise PlanError("min_stop_savings must be a number >= 0.")
    tank = settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG
    start_given = start_fuel_gallons is not None
    if not start_given:
        start_fuel_gallons = settings.DEFAULT_START_FUEL_GALLONS
    if not (math.isfinite(start_fuel_gallons) and 0 <= start_fuel_gallons <= tank):
        raise PlanError(f"start_fuel_gallons must be between 0 and {tank:g}.")

    try:
        a, b = geo.resolve(start), geo.resolve(finish)
    except geo.LocationError as exc:
        raise PlanError(str(exc), exc.status) from exc

    try:
        index = get_index()  # one consistent snapshot for the whole request
    except DatasetChanging as exc:
        raise PlanError(str(exc), 503) from exc
    key_src = f"{index.version}|{a.lat:.5f},{a.lon:.5f}|{b.lat:.5f},{b.lon:.5f}|{min_stop_savings:g}|{start_fuel_gallons}"
    cache_key = "plan:" + hashlib.sha1(key_src.encode()).hexdigest()
    result = cache.get(cache_key)
    cached = result is not None
    if not cached:
        result = _compute(a, b, min_stop_savings, start_fuel_gallons, start_given, index)
        cache.set(cache_key, result, settings.ROUTE_CACHE_SECONDS)

    out = dict(result)
    if not include_geometry:
        out["route"] = {k: v for k, v in out["route"].items() if k != "geometry"}
    out["start"], out["finish"] = a.as_dict(), b.as_dict()
    meta = dict(out["meta"])
    routing_calls = meta.pop("routing_calls")
    meta.update(
        cached=cached,
        external_calls={
            "routing": 0 if cached else routing_calls,
            "geocoding": sum(loc.source == "nominatim" for loc in (a, b)),
        },
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )
    out["meta"] = meta
    return out


def _route_match(c, route_highways, near_route, window):
    """Evidence (not proof) that a station near the route can be used from it.

    * its address names a highway the route drives near that point (likely an exit
      of the road we're on; exit, direction and detour are not verified); or
    * its city centroid is within `near_route` miles of the route line.
    Returns a description of the evidence, or None to reject the station.
    """
    if c.highways:
        shared = c.highways & highways.refs_near(route_highways, c.mile, window)
        if shared:
            return f"address names {', '.join(sorted(shared))}, which the route uses nearby"
    if c.offset_miles <= near_route:
        return f"city centre within {near_route:g} mi of the route"
    return None


def _compute(a, b, min_stop_savings, start_fuel_gallons, start_given, index) -> dict:
    try:
        route = routing.get_route(a.lat, a.lon, b.lat, b.lon)
    except routing.RoutingError as exc:
        raise PlanError(str(exc), exc.status) from exc

    max_range = settings.VEHICLE_MAX_RANGE_MILES
    mpg = settings.VEHICLE_MPG
    tank = max_range / mpg
    corridor = settings.STATION_CORRIDOR_MILES
    near_route = settings.STATION_NEAR_ROUTE_MILES

    # Scale geometry miles to the router's official distance so mile markers line up
    # with the step distances used for highway matching.
    found, cum = index.along_route(route.coordinates, corridor)
    geom_miles = float(cum[-1]) if len(cum) else 0.0
    scale = route.distance_miles / geom_miles if geom_miles else 1.0
    candidates, matches = [], {}
    for c in found:
        c.mile *= scale
        how = _route_match(c, route.highways or [], near_route, corridor)
        if how:
            candidates.append(c)
            matches[c.station_id] = how

    try:
        cheapest = plan_fuel_stops(
            ((c.station_id, c.mile, c.price) for c in candidates),
            total_miles=route.distance_miles,
            max_range_miles=max_range,
            mpg=mpg,
            start_fuel_gallons=start_fuel_gallons,
        )
    except NoFeasiblePlan as exc:
        hint = "" if start_given else " Pass start_fuel_gallons if the vehicle starts with more fuel."
        raise PlanError(str(exc) + hint, 422) from exc
    plan = consolidate_stops(copy.deepcopy(cheapest), mpg, tank, min_stop_savings)

    offsets = {c.station_id: c.offset_miles for c in candidates}
    stops = []
    for n, s in enumerate(plan.stops, start=1):
        st = index.info[s.key]  # same snapshot the plan was computed from
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
                "route_match": matches[s.key],
                "city_centroid_distance_from_route_miles": round(offsets.get(s.key, 0.0), 1),
                "fuel_on_arrival_gallons": round(s.fuel_on_arrival_gal, 2),
                "gallons_purchased": round(s.gallons, 2),
                "cost": round(s.cost, 2),
            }
        )

    avg = plan.total_cost / plan.total_gallons if plan.total_gallons else 0.0
    start_mode = (
        f"{start_fuel_gallons:g} gallons (given by the caller)"
        if start_given
        else f"{start_fuel_gallons:g} gallons (default; pass start_fuel_gallons to change)"
    )
    return {
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
            "fuel_purchased_gallons": round(plan.total_gallons, 2),
            "fuel_consumed_gallons": round(route.distance_miles / mpg, 2),
            "starting_fuel_gallons": round(plan.start_fuel_gallons, 2),
            "fuel_left_at_arrival_gallons": round(plan.end_fuel_gallons, 2),
            "average_price_per_gallon": round(avg, 4),
            "number_of_stops": len(stops),
            "stations_considered": len(candidates),
            "cheapest_possible_cost": round(cheapest.total_cost, 2),
            "cheapest_possible_stops": len(cheapest.stops),
            "extra_cost_for_fewer_stops": round(plan.total_cost - cheapest.total_cost, 2),
            "is_estimate": True,
        },
        "meta": {
            "assumptions": {
                "max_range_miles": max_range,
                "miles_per_gallon": mpg,
                "tank_gallons": tank,
                "starting_fuel": start_mode,
                "total_fuel_cost": "money spent on fuel purchased during the trip; the starting fuel is not included",
                "station_location": (
                    "city centroid (the price file has no coordinates). A station is used if its address "
                    "names a highway the route uses nearby, or its city is within "
                    f"{near_route:g} miles of the route. This is evidence, not verification: the exact exit, "
                    "access direction and detour are unknown, so mile markers, costs and range feasibility "
                    "are estimates."
                ),
            },
            "min_stop_savings": min_stop_savings,
            "station_dataset_version": index.version,
            "routing_provider": settings.OSRM_URL,
            "routing_calls": 0 if route.from_cache else 1,
        },
    }
