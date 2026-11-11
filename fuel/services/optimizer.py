"""Cheapest refuelling plan along a fixed route (the "gas station problem").

Stations are points on a line (miles from the start) with a price per gallon.
The vehicle has a fixed range on a full tank. Greedy rule, applied at each stop:

  * If a cheaper station (or the destination) is within range, buy only enough
    fuel to reach the first such place and go there.
  * Otherwise fill the tank and go to the cheapest station within range.

With linear prices and no detour cost this greedy plan is optimal: every
mile is fuelled by the cheapest station that could have reached it.
"""

from dataclasses import dataclass, field

EPS = 1e-9


class NoFeasiblePlan(ValueError):
    """No plan exists. `code` says why, for API clients:

    no_stations_on_route   the price data has no usable station along the route
    no_station_near_start  pay-for-all-fuel start, but no station within the approach distance
    start_fuel_too_low     the given starting fuel can't reach any station
    range_gap              two consecutive stations (or the last one and the finish) are out of range
    """

    def __init__(self, message, gap_start_mile=None, code="range_gap"):
        super().__init__(message)
        self.gap_start_mile = gap_start_mile
        self.code = code


@dataclass
class Stop:
    key: object  # caller's identifier for the station
    mile: float
    price: float
    gallons: float
    cost: float
    fuel_on_arrival_gal: float


@dataclass
class Plan:
    stops: list = field(default_factory=list)
    total_gallons: float = 0.0  # purchased
    total_cost: float = 0.0  # stop purchases + start_fuel_cost
    start_fuel_gallons: float = 0.0  # in the tank at mile 0
    end_fuel_gallons: float = 0.0  # left in the tank on arrival
    start_fuel_cost: float = 0.0  # cost of the starting fuel when billed (empty start), else 0


def plan_fuel_stops(stations, total_miles, max_range_miles, mpg, start_fuel_gallons=None, approach_miles=30.0):
    """
    stations: iterable of (key, mile, price). Only stations with 0 <= mile <= total_miles are used.
    start_fuel_gallons: fuel already in the tank at mile 0, not paid for on this trip.
        None (the default) means "pay for all fuel": the trip pays for every gallon it
        burns. The price file has no station exactly at the start, so the vehicle is
        assumed to start with just enough fuel to reach its first stop, which may be any
        station within `approach_miles`; that fuel is charged at the first stop's price.
        Each such first stop is tried and the cheapest whole-trip plan wins. With no
        station that close, there is no plan (the estimate would no longer be honest).
    Every purchase fits in the tank: fuel on arrival + gallons bought <= tank size.
    Returns a Plan, or raises NoFeasiblePlan if some gap is longer than the range.
    """
    if max_range_miles <= 0 or mpg <= 0:
        raise ValueError("max_range_miles and mpg must be positive")
    tank = max_range_miles / mpg
    billed_start = start_fuel_gallons is None
    if not billed_start and not (0 <= start_fuel_gallons <= tank + EPS):
        raise ValueError(f"start_fuel_gallons must be between 0 and {tank:g}")
    if total_miles < 0:
        raise ValueError("total_miles must be >= 0")
    if total_miles <= EPS:  # zero-distance trip: nothing to buy
        start = 0.0 if billed_start else start_fuel_gallons
        return Plan(start_fuel_gallons=start, end_fuel_gallons=start)

    # Keep the cheapest station at each mile marker; sort by position.
    best_at = {}
    for key, mile, price in stations:
        if -EPS <= mile <= total_miles + EPS:
            m = round(max(0.0, min(mile, total_miles)), 6)
            if m not in best_at or price < best_at[m][2]:
                best_at[m] = (key, m, float(price))
    pts = sorted(best_at.values(), key=lambda s: s[1])

    if billed_start:
        # A first stop must be before the destination (buying there buys nothing).
        before_end = [j for j, p in enumerate(pts) if p[1] < total_miles - EPS]
        if not before_end:
            raise NoFeasiblePlan("The price file has no fuel station along this route.", 0.0, "no_stations_on_route")
        firsts = [j for j in before_end if pts[j][1] <= approach_miles + EPS]
        if not firsts:
            raise NoFeasiblePlan(
                f"The price file has no fuel station within {approach_miles:g} miles of the start "
                f"(the first one on the route is at mile {pts[before_end[0]][1]:.0f}).",
                0.0,
                "no_station_near_start",
            )
        best, last_err = None, None
        for j in firsts:
            try:
                cand = _plan_from(pts[j:], total_miles, max_range_miles, mpg, pts[j][1], billed_price=pts[j][2])
            except NoFeasiblePlan as exc:
                last_err = exc
                continue
            if (best is None or cand.total_cost < best.total_cost - EPS
                    or (cand.total_cost <= best.total_cost + EPS and len(cand.stops) < len(best.stops))):
                best = cand
        if best is None:
            raise last_err
        return best
    return _plan_from(pts, total_miles, max_range_miles, mpg, start_fuel_gallons * mpg)


def _plan_from(pts, total_miles, max_range_miles, mpg, start_miles, billed_price=None):
    """Greedy plan over sorted, de-duplicated `pts`, starting with `start_miles` of fuel.

    With `billed_price`, the starting fuel is charged at that price (empty-start mode,
    where start_miles is exactly the distance to pts[0]).
    """
    bill = Plan(start_fuel_gallons=start_miles / mpg)
    if billed_price is not None:
        bill.start_fuel_cost = bill.start_fuel_gallons * billed_price
    if start_miles + EPS >= total_miles:  # the starting fuel covers the whole trip
        bill.end_fuel_gallons = max(0.0, start_miles - total_miles) / mpg
        bill.total_cost = bill.start_fuel_cost
        return bill

    reachable = [k for k, s in enumerate(pts) if s[1] <= start_miles + EPS]
    if not reachable:
        first = f"mile {pts[0][1]:.0f}" if pts else "none on the route"
        raise NoFeasiblePlan(
            f"No fuel station within {start_miles:.0f} miles of the start, the range of the "
            f"{start_miles / mpg:g} gallons in the tank (nearest: {first}).",
            0.0,
            "start_fuel_too_low" if pts else "no_stations_on_route",
        )
    # The start acts as a station we can't buy at, so every station is "cheaper":
    # the greedy rule says go to the first one.
    i = reachable[0]

    plan = bill
    plan.total_cost = plan.start_fuel_cost
    fuel = start_miles - pts[i][1]  # range left on arrival (miles)
    n = len(pts)

    while True:
        key, here, price = pts[i]
        horizon = here + max_range_miles

        # First strictly cheaper station within range (or the destination, which is "free").
        nxt = next((j for j in range(i + 1, n) if pts[j][1] > horizon + EPS or pts[j][2] < price - EPS), None)
        if nxt is not None and pts[nxt][1] > horizon + EPS:
            nxt = None
        if nxt is None and total_miles <= horizon + EPS:
            buy = max(0.0, total_miles - here - fuel)
            _add_stop(plan, key, here, price, buy, fuel, mpg)
            plan.end_fuel_gallons = (fuel + buy - (total_miles - here)) / mpg
            break
        if nxt is not None:
            buy = max(0.0, pts[nxt][1] - here - fuel)
            _add_stop(plan, key, here, price, buy, fuel, mpg)
            fuel = fuel + buy - (pts[nxt][1] - here)
            i = nxt
            continue

        # Nothing cheaper in range: fill up and go to the cheapest station in range.
        in_range = [j for j in range(i + 1, n) if pts[j][1] <= horizon + EPS]
        if not in_range:
            raise NoFeasiblePlan(
                f"No fuel station within {max_range_miles:.0f} miles after mile {here:.0f}; "
                "the route cannot be completed on this vehicle's range.",
                here,
            )
        j = min(in_range, key=lambda k: (pts[k][2], -pts[k][1]))
        buy = max_range_miles - fuel
        _add_stop(plan, key, here, price, buy, fuel, mpg)
        fuel = max_range_miles - (pts[j][1] - here)
        i = j

    return plan


def _add_stop(plan, key, mile, price, buy_miles, fuel_miles, mpg):
    if buy_miles <= EPS:
        return
    gallons = buy_miles / mpg
    cost = gallons * price
    plan.stops.append(Stop(key, mile, price, gallons, cost, fuel_miles / mpg))
    plan.total_gallons += gallons
    plan.total_cost += cost


def simulate(stops, mpg, start_fuel_gallons):
    """Fuel (gallons) on arrival at each stop, starting with `start_fuel_gallons` at mile 0."""
    arrivals, fuel, last = [], start_fuel_gallons, 0.0
    for s in stops:
        fuel -= (s.mile - last) / mpg
        arrivals.append(fuel)
        fuel += s.gallons
        last = s.mile
    return arrivals


def consolidate_stops(plan, mpg, tank_gallons, min_stop_savings):
    """Drop fuel stops that save less than `min_stop_savings` dollars.

    The cheapest plan often makes tiny top-ups (e.g. 1 gallon at one station,
    then a fill-up 12 miles later at a slightly cheaper one). A stop is removed
    by moving its purchase to the previous stop (if the tank has room) or to the
    next stop (if the vehicle can reach it without that fuel). The cheapest such
    removal is applied repeatedly while it costs less than `min_stop_savings`.
    The first stop is never removed when the tank arrives there empty.
    """
    stops = plan.stops
    if min_stop_savings <= 0 or len(stops) < 2:
        return plan

    while True:
        arrivals = simulate(stops, mpg, plan.start_fuel_gallons)
        best = None  # (extra_cost, k, target)
        for k in range(len(stops)):
            g = stops[k].gallons
            prev = stops[k - 1] if k else None
            if prev and arrivals[k - 1] + prev.gallons + g <= tank_gallons + EPS:
                best = min(best or (float("inf"),), (g * (prev.price - stops[k].price), k, k - 1))
            if k + 1 < len(stops) and arrivals[k + 1] - g >= -EPS:
                best = min(best or (float("inf"),), (g * (stops[k + 1].price - stops[k].price), k, k + 1))
        if best is None or best[0] >= min_stop_savings:
            break
        _, k, target = best
        moved = stops.pop(k)
        t = stops[target if target < k else target - 1]
        t.gallons += moved.gallons
        t.cost = t.gallons * t.price

    for s, arrival in zip(stops, simulate(stops, mpg, plan.start_fuel_gallons)):
        s.fuel_on_arrival_gal = max(0.0, arrival)
    plan.total_cost = plan.start_fuel_cost + sum(s.cost for s in stops)
    plan.total_gallons = sum(s.gallons for s in stops)
    return plan
