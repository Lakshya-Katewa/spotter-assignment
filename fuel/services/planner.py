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
    """Bad input (400), an impossible trip (422) or an unavailable upstream (502/503).

    `code` is a stable machine-readable reason for 422s; `partial` carries what could
    still be computed (the route), so clients can show the map even without a plan.
    """

    def __init__(self, message, status=400, code=None, partial=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.partial = partial


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
    if start_fuel_gallons is None:
        start_fuel_gallons = settings.DEFAULT_START_FUEL_GALLONS
    # None = pay for all the fuel the trip burns (see plan_fuel_stops).
    if start_fuel_gallons is not None and not (math.isfinite(start_fuel_gallons) and 0 <= start_fuel_gallons <= tank):
        raise PlanError(f"start_fuel_gallons must be between 0 and {tank:g}.")

    try:
        a, b = geo.resolve(start), geo.resolve(finish)
    except geo.LocationError as exc:
        raise PlanError(str(exc), exc.status) from exc

    try:
        index = get_index()  # one consistent snapshot for the whole request
    except DatasetChanging as exc:
        raise PlanError(str(exc), 503) from exc
    key_src = f"v2|{index.version}|{a.lat:.5f},{a.lon:.5f}|{b.lat:.5f},{b.lon:.5f}|{min_stop_savings:g}|{start_fuel_gallons}|{settings.START_APPROACH_MILES:g}"
    cache_key = "plan:" + hashlib.sha1(key_src.encode()).hexdigest()
    result = cache.get(cache_key)
    cached = result is not None
    if not cached:
        try:
            result = _compute(a, b, min_stop_savings, start_fuel_gallons, index)
        except PlanError as exc:
            if exc.partial:
                exc.partial.update(start=a.as_dict(), finish=b.as_dict())
                if not include_geometry:
                    exc.partial["route"].pop("geometry", None)
            raise
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


def _route_match(c, info, route_highways, near_route, exit_near_route, window):
    """Evidence that a station near the route can be used from it, or None to reject it.

    * Station placed at its matched interstate exit (OSM junction): it must be within
      `exit_near_route` miles of the route. Position-based, the strongest evidence we
      have, though the pump itself may sit a short way from the junction.
    * Station placed at its city centroid (no matchable exit): its address names a
      highway the route uses nearby, or the centroid is within `near_route` miles.
    Neither verifies access direction or the exact detour.
    """
    if info.location_precision == "exit":
        if c.offset_miles <= exit_near_route:
            return f"matched to {info.exit_label}, {c.offset_miles:.1f} mi from the route"
        return None
    if c.highways:
        shared = c.highways & highways.refs_near(route_highways, c.mile, window)
        if shared:
            return f"address names {', '.join(sorted(shared))}, which the route uses nearby"
    if c.offset_miles <= near_route:
        return f"city centre within {near_route:g} mi of the route"
    return None


def _route_json(route) -> dict:
    return {
        "distance_miles": round(route.distance_miles, 1),
        "duration_hours": round(route.duration_seconds / 3600, 2),
        "geometry": {
            "type": "LineString",
            "coordinates": [[round(x, 5), round(y, 5)] for x, y in simplify(route.coordinates)],
        },
    }


def _compute(a, b, min_stop_savings, start_fuel_gallons, index) -> dict:
    try:
        route = routing.get_route(a.lat, a.lon, b.lat, b.lon)
    except routing.RoutingError as exc:
        raise PlanError(str(exc), exc.status) from exc

    max_range = settings.VEHICLE_MAX_RANGE_MILES
    mpg = settings.VEHICLE_MPG
    tank = max_range / mpg
    corridor = settings.STATION_CORRIDOR_MILES
    near_route = settings.STATION_NEAR_ROUTE_MILES
    exit_near_route = settings.EXIT_NEAR_ROUTE_MILES

    # Scale geometry miles to the router's official distance so mile markers line up
    # with the step distances used for highway matching.
    found, cum = index.along_route(route.coordinates, corridor)
    geom_miles = float(cum[-1]) if len(cum) else 0.0
    scale = route.distance_miles / geom_miles if geom_miles else 1.0
    candidates, matches = [], {}
    for c in found:
        c.mile *= scale
        how = _route_match(c, index.info[c.station_id], route.highways or [], near_route, exit_near_route, corridor)
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
            approach_miles=settings.START_APPROACH_MILES,
        )
    except NoFeasiblePlan as exc:
        hint = ""
        if start_fuel_gallons is None and exc.code in ("no_stations_on_route", "no_station_near_start"):
            hint = (
                " If the vehicle departs with fuel in the tank, pass start_fuel_gallons "
                f"(up to {tank:g}; a full tank covers {max_range:g} miles)."
            )
        raise PlanError(str(exc) + hint, 422, code=exc.code, partial={"route": _route_json(route)}) from exc
    plan = consolidate_stops(copy.deepcopy(cheapest), mpg, tank, min_stop_savings)
    # The cheapest plan often includes tiny top-ups (cents of savings each). Report what
    # a practical threshold would give, so the trade-off is visible without asking.
    alt_threshold = settings.FEWER_STOPS_SUGGESTION_SAVINGS
    alternative = None
    if min_stop_savings < alt_threshold:
        alt = consolidate_stops(copy.deepcopy(cheapest), mpg, tank, alt_threshold)
        if len(alt.stops) < len(plan.stops):
            alternative = {
                "min_stop_savings": alt_threshold,
                "number_of_stops": len(alt.stops),
                "total_fuel_cost": round(alt.total_cost, 2),
                "extra_cost": round(alt.total_cost - plan.total_cost, 2),
            }

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
                "location_precision": "highway exit" if st.location_precision == "exit" else "city centre",
                "route_match": matches[s.key],
                "distance_from_route_miles": round(offsets.get(s.key, 0.0), 1),
                "fuel_on_arrival_gallons": round(s.fuel_on_arrival_gal, 2),
                "gallons_purchased": round(s.gallons, 2),
                "cost": round(s.cost, 2),
            }
        )

    billed_start = start_fuel_gallons is None
    paid_gallons = plan.total_gallons + (plan.start_fuel_gallons if billed_start else 0.0)
    avg = plan.total_cost / paid_gallons if paid_gallons else 0.0
    if billed_start:
        start_mode = (
            "pay for all fuel (default): the vehicle starts with just enough fuel to reach its first "
            f"stop (within {settings.START_APPROACH_MILES:g} miles), and that fuel is charged at the "
            "first stop's price. Pass start_fuel_gallons for fuel already in the tank (not charged)."
        )
        cost_note = "all fuel the trip burns (distance / mpg), at the prices of the planned stops"
    else:
        start_mode = f"{start_fuel_gallons:g} gallons already in the tank (given by the caller; not paid for on this trip)"
        cost_note = "fuel purchased during the trip; the fuel already in the tank at the start is not included"

    return {
        "route": _route_json(route),
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(plan.total_cost, 2),
            "fuel_paid_for_gallons": round(paid_gallons, 2),
            "fuel_purchased_gallons": round(plan.total_gallons, 2),
            "fuel_consumed_gallons": round(route.distance_miles / mpg, 2),
            "starting_fuel_gallons": round(plan.start_fuel_gallons, 2),
            "starting_fuel_billed": billed_start,
            "starting_fuel_cost": round(plan.start_fuel_cost, 2),
            "fuel_left_at_arrival_gallons": round(plan.end_fuel_gallons, 2),
            "average_price_per_gallon": round(avg, 4),
            "number_of_stops": len(stops),
            "stops_located_at_exit": sum(st["location_precision"] == "highway exit" for st in stops),
            "stations_considered": len(candidates),
            "cheapest_possible_cost": round(cheapest.total_cost, 2),
            "cheapest_possible_stops": len(cheapest.stops),
            "extra_cost_for_fewer_stops": round(plan.total_cost - cheapest.total_cost, 2),
            "fewer_stops_alternative": alternative,
            "is_estimate": True,
        },
        "meta": {
            "assumptions": {
                "max_range_miles": max_range,
                "miles_per_gallon": mpg,
                "tank_gallons": tank,
                "starting_fuel": start_mode,
                "total_fuel_cost": cost_note,
                "station_location": (
                    "The price file has no coordinates. Stations whose address names an interstate exit "
                    "are placed at that exit (OpenStreetMap) and used if within "
                    f"{exit_near_route:g} mi of the route; the rest are placed at their city centroid and used "
                    "if their address names a highway the route uses nearby, or the centroid is within "
                    f"{near_route:g} mi. Access direction and detours are not verified, so costs and range "
                    "feasibility are estimates (city-centre stops more so)."
                ),
            },
            "min_stop_savings": min_stop_savings,
            "station_dataset_version": index.version,
            "routing_provider": settings.OSRM_URL,
            "attribution": "Routes and interstate exit positions: (c) OpenStreetMap contributors, ODbL. "
                           "Places: US Census Bureau.",
            "routing_calls": 0 if route.from_cache else 1,
        },
    }
