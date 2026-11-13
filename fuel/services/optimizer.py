"""Cheapest refuelling plan along a fixed route (the "gas station problem").

Stations are points on a line (miles from the start) with a price per gallon.
The vehicle has a fixed range on a full tank. Greedy rule, applied at each stop:

  * If a cheaper station (or the destination) is within range, buy only enough
    fuel to reach the first such place and go there.
  * Otherwise fill the tank and go to the cheapest station within range.

With linear prices and no detour cost this greedy plan is optimal: every
mile is fuelled by the cheapest station that could have reached it.

Complexity, for n stations: O(n log n) to precompute (see _Lookahead), then
O(log n) per greedy step and at most n steps. A naive version that scans ahead
from every stop is O(n * stations in range) per plan, and the pay-for-all-fuel
mode runs one plan per possible first stop.
"""

from bisect import bisect_right
from dataclasses import dataclass, field

EPS = 1e-9


class NoFeasiblePlan(ValueError):
    """No plan exists. `code` says why, for API clients:

    no_stations_on_route   the price data has no usable station along the route
    no_station_near_start  pay-for-all-fuel start, but the first station is beyond the range
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


class _Lookahead:
    """Precomputed answers to the two questions the greedy rule asks at every stop.

    pts: (key, mile, price) sorted by mile, at most one station per mile.

    * next_cheaper[i]: the first station after i that is strictly cheaper, found for
      all i in one right-to-left pass with a monotonic stack: O(n).
    * cheapest(lo, hi): the cheapest station in pts[lo..hi] (ties: the farthest), from
      a sparse table of range minimums: O(n log n) to build, O(1) per query.
    * The stations within range of a stop are a contiguous run (pts is sorted by
      mile), so its end is found by binary search: O(log n).
    """

    def __init__(self, pts):
        self.pts = pts
        self.miles = [p[1] for p in pts]
        n = len(pts)

        self.next_cheaper = [n] * n
        stack = []  # indices to the right of i, prices strictly increasing from top to bottom
        for i in range(n - 1, -1, -1):
            while stack and pts[stack[-1]][2] >= pts[i][2] - EPS:  # not cheaper than i: never the answer again
                stack.pop()
            self.next_cheaper[i] = stack[-1] if stack else n
            stack.append(i)

        self.table = [list(range(n))]  # table[k][i] = cheapest index in pts[i : i + 2**k]
        width = 1
        while 2 * width <= n:
            prev = self.table[-1]
            self.table.append([self._better(prev[i], prev[i + width]) for i in range(n - 2 * width + 1)])
            width *= 2

    def _better(self, a, b):
        """Cheaper station; on a price tie the farther one (it leaves more options ahead)."""
        pa, pb = self.pts[a], self.pts[b]
        return a if (pa[2], -pa[1]) <= (pb[2], -pb[1]) else b

    def cheapest(self, lo, hi):
        level = (hi - lo + 1).bit_length() - 1
        return self._better(self.table[level][lo], self.table[level][hi - (1 << level) + 1])

    def last_within(self, mile):
        """Index of the last station at or before `mile` (-1 if none)."""
        return bisect_right(self.miles, mile + EPS) - 1


@dataclass
class Plan:
    stops: list = field(default_factory=list)
    total_gallons: float = 0.0  # purchased
    total_cost: float = 0.0  # stop purchases + start_fuel_cost
    start_fuel_gallons: float = 0.0  # in the tank at mile 0
    end_fuel_gallons: float = 0.0  # left in the tank on arrival
    start_fuel_cost: float = 0.0  # cost of the starting fuel when billed (pay-for-all-fuel start), else 0


def plan_fuel_stops(stations, total_miles, max_range_miles, mpg, start_fuel_gallons=None, approach_miles=30.0):
    """
    stations: iterable of (key, mile, price). Only stations with 0 <= mile <= total_miles are used.
    start_fuel_gallons: fuel already in the tank at mile 0, not paid for on this trip.
        None (the default) means "pay for all fuel": the trip pays for every gallon it
        burns. The price file has no station exactly at the start, so the vehicle is
        assumed to start with just enough fuel to reach its first stop, which may be any
        station within `approach_miles`; that fuel is charged at the first stop's price.
        Each such first stop is tried and the cheapest whole-trip plan wins. If no station
        is that close (the price file has none around Houston, for example), the first
        station on the route is the first stop; the caller should flag that estimate.
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
    look = _Lookahead(pts)  # built once, shared by every first-stop candidate

    if billed_start:
        # A first stop must be before the destination (buying there buys nothing).
        before_end = [j for j, p in enumerate(pts) if p[1] < total_miles - EPS]
        if not before_end:
            raise NoFeasiblePlan("The price file has no fuel station along this route.", 0.0, "no_stations_on_route")
        first_mile = pts[before_end[0]][1]
        if first_mile > max_range_miles + EPS:
            raise NoFeasiblePlan(
                f"The first fuel station on the route is at mile {first_mile:.0f}, beyond the "
                f"{max_range_miles:.0f}-mile range of a full tank.",
                0.0,
                "no_station_near_start",
            )
        firsts = [j for j in before_end if pts[j][1] <= max(approach_miles, first_mile) + EPS]
        best, last_err = None, None
        for j in firsts:
            try:
                cand = _plan_from(look, j, total_miles, max_range_miles, mpg, pts[j][1], billed_price=pts[j][2])
            except NoFeasiblePlan as exc:
                last_err = exc
                continue
            if (best is None or cand.total_cost < best.total_cost - EPS
                    or (cand.total_cost <= best.total_cost + EPS and len(cand.stops) < len(best.stops))):
                best = cand
        if best is None:
            raise last_err
        return best
    return _plan_from(look, 0, total_miles, max_range_miles, mpg, start_fuel_gallons * mpg)


def _plan_from(look, first, total_miles, max_range_miles, mpg, start_miles, billed_price=None):
    """Greedy plan over the sorted, de-duplicated stations look.pts[first:], starting
    with `start_miles` of fuel.

    With `billed_price`, the starting fuel is charged at that price (pay-for-all-fuel mode,
    where start_miles is exactly the distance to look.pts[first]).
    """
    pts = look.pts
    bill = Plan(start_fuel_gallons=start_miles / mpg)
    if billed_price is not None:
        bill.start_fuel_cost = bill.start_fuel_gallons * billed_price
    if start_miles + EPS >= total_miles:  # the starting fuel covers the whole trip
        bill.end_fuel_gallons = max(0.0, start_miles - total_miles) / mpg
        bill.total_cost = bill.start_fuel_cost
        return bill

    n = len(pts)
    if first >= n or pts[first][1] > start_miles + EPS:
        nearest = f"mile {pts[first][1]:.0f}" if first < n else "none on the route"
        raise NoFeasiblePlan(
            f"No fuel station within {start_miles:.0f} miles of the start, the range of the "
            f"{start_miles / mpg:g} gallons in the tank (nearest: {nearest}).",
            0.0,
            "start_fuel_too_low" if first < n else "no_stations_on_route",
        )
    # The start acts as a station we can't buy at, so every station is "cheaper":
    # the greedy rule says go to the first one.
    i = first

    plan = bill
    plan.total_cost = plan.start_fuel_cost
    fuel = start_miles - pts[i][1]  # range left on arrival (miles)

    while True:
        key, here, price = pts[i]
        horizon = here + max_range_miles
        last = look.last_within(horizon)  # stations i+1..last are within range

        # First strictly cheaper station within range (or the destination, which is "free").
        nxt = look.next_cheaper[i]
        if nxt > last:
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
        if last <= i:
            raise NoFeasiblePlan(
                f"No fuel station within {max_range_miles:.0f} miles after mile {here:.0f}; "
                "the route cannot be completed on this vehicle's range.",
                here,
            )
        j = look.cheapest(i + 1, last)
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
