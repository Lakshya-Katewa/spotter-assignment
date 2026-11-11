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
    def __init__(self, message, gap_start_mile=None):
        super().__init__(message)
        self.gap_start_mile = gap_start_mile


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
    total_cost: float = 0.0  # spent at the pumps
    start_fuel_gallons: float = 0.0  # in the tank at mile 0 (not purchased on this trip)
    end_fuel_gallons: float = 0.0  # left in the tank on arrival


def plan_fuel_stops(stations, total_miles, max_range_miles, mpg, start_fuel_gallons=0.0):
    """
    stations: iterable of (key, mile, price). Only stations with 0 <= mile <= total_miles are used.
    start_fuel_gallons: fuel in the tank at mile 0. It is a fixed input, independent
        of which stations are chosen, and is not counted as a purchase.
    Every purchase fits in the tank: fuel on arrival + gallons bought <= tank size.
    Returns a Plan, or raises NoFeasiblePlan if some gap is longer than the range.
    """
    if max_range_miles <= 0 or mpg <= 0:
        raise ValueError("max_range_miles and mpg must be positive")
    tank = max_range_miles / mpg
    if not (0 <= start_fuel_gallons <= tank + EPS):
        raise ValueError(f"start_fuel_gallons must be between 0 and {tank:g}")
    if total_miles < 0:
        raise ValueError("total_miles must be >= 0")

    start_miles = start_fuel_gallons * mpg
    if start_miles + EPS >= total_miles:  # includes zero-distance trips: nothing to buy
        return Plan(start_fuel_gallons=start_fuel_gallons, end_fuel_gallons=max(0.0, start_miles - total_miles) / mpg)

    # Keep the cheapest station at each mile marker; sort by position.
    best_at = {}
    for key, mile, price in stations:
        if -EPS <= mile <= total_miles + EPS:
            m = round(max(0.0, min(mile, total_miles)), 6)
            if m not in best_at or price < best_at[m][2]:
                best_at[m] = (key, m, float(price))
    pts = sorted(best_at.values(), key=lambda s: s[1])

    reachable = [k for k, s in enumerate(pts) if s[1] <= start_miles + EPS]
    if not reachable:
        first = f"mile {pts[0][1]:.0f}" if pts else "none on the route"
        raise NoFeasiblePlan(
            f"No fuel station within {start_miles:.0f} miles of the start, the range of the "
            f"{start_fuel_gallons:g} gallons in the tank (nearest: {first}).",
            0.0,
        )
    # The start acts as a station we can't buy at, so every station is "cheaper":
    # the greedy rule says go to the first one.
    i = reachable[0]

    plan = Plan(start_fuel_gallons=start_miles / mpg)
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
    The first stop is never removed, because the tank starts empty there.
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
    plan.total_cost = sum(s.cost for s in stops)
    plan.total_gallons = sum(s.gallons for s in stops)
    return plan
