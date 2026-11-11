"""Shared fixtures and helpers for the test modules."""

import math
import threading
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.models import DatasetVersion, FuelStation, Place
from fuel.services import routing
from fuel.services.names import place_key
from fuel.services.optimizer import simulate
from fuel.services.stations import reset_index

TANK = 50  # gallons: 500-mile range at 10 mpg



# -- helpers -----------------------------------------------------------------
def brute_force_cost(stations, total, rng, mpg, start_fuel):
    """Exact optimum by DP over (station, integer fuel miles). Small inputs only."""
    pts = sorted(stations, key=lambda s: s[1])
    best = {}

    def go(i, fuel):
        if (i, fuel) in best:
            return best[(i, fuel)]
        _, here, price = pts[i]
        result = math.inf
        if total - here <= rng:
            result = max(0, total - here - fuel) / mpg * price
        for j in range(i + 1, len(pts)):
            dist = pts[j][1] - here
            if dist > rng:
                break
            for buy in range(max(0, dist - fuel), rng - fuel + 1):
                result = min(result, buy / mpg * price + go(j, fuel + buy - dist))
        best[(i, fuel)] = result
        return result

    return min(go(i, start_fuel - pts[i][1]) for i in range(len(pts)) if pts[i][1] <= start_fuel)



def assert_feasible(test, plan, total_miles, mpg=10, tank=TANK):
    """Drive the plan: never run dry, never overfill, and fuel adds up."""
    arrivals = simulate(plan.stops, mpg, plan.start_fuel_gallons)
    for k, (s, arrival) in enumerate(zip(plan.stops, arrivals)):
        test.assertGreaterEqual(arrival, -1e-6, f"ran dry before stop {k}")
        test.assertLessEqual(arrival + s.gallons, tank + 1e-6, f"overfilled at stop {k}")
    last_mile, fuel = (plan.stops[-1].mile, arrivals[-1] + plan.stops[-1].gallons) if plan.stops else (0, plan.start_fuel_gallons)
    test.assertAlmostEqual(fuel - (total_miles - last_mile) / mpg, plan.end_fuel_gallons, places=6)
    test.assertAlmostEqual(plan.start_fuel_gallons + plan.total_gallons - plan.end_fuel_gallons, total_miles / mpg, places=6)



# -- routing HTTP client -----------------------------------------------------
def osrm_response(status=200, payload=None, json_error=False):
    r = mock.Mock(status_code=status)
    if json_error:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r



OSRM_OK = {
    "code": "Ok",
    "routes": [{
        "distance": 160934.4,  # 100 miles
        "duration": 5400,
        "geometry": "_p~iF~ps|U_ulLnnqC",
        "legs": [{"steps": [{"distance": 120701, "ref": "I 5; US 99"}, {"distance": 40233.4, "ref": ""}]}],
    }],
}



class NoRateLimitMixin:
    """Tests not about rate limiting skip the 1 request/second wait between mocked calls."""

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = mock.patch("fuel.services.ratelimit.acquire")
        patcher.start()
        self.addCleanup(patcher.stop)



# -- geocoding ---------------------------------------------------------------
def nominatim(lat, lon, label="Somewhere, USA"):
    r = mock.Mock(status_code=200)
    r.raise_for_status.return_value = None
    r.json.return_value = [{"lat": str(lat), "lon": str(lon), "display_name": label}] if lat is not None else []
    return r



# -- API ---------------------------------------------------------------------
def fake_route(*_args, **_kwargs):
    # Straight line north along -100W from 40N; the route is "I-99" the whole way.
    return routing.Route(
        distance_miles=691.0,
        duration_seconds=36000,
        coordinates=[[-100.0, 40.0], [-100.0, 50.0]],
        highways=[(0.0, 691.0, frozenset({"I99"}))],
    )



@override_settings(DEFAULT_START_FUEL_GALLONS=3, STATION_CORRIDOR_MILES=15, STATION_NEAR_ROUTE_MILES=2, MIN_STOP_SAVINGS=0)
class ApiFixture(TestCase):
    """Shared stations along a fake north-south "I-99" route."""

    @classmethod
    def setUpTestData(cls):
        Place.objects.create(key=place_key("Southtown", "NE"), name="Southtown", state="NE", lat=40.0, lon=-100.0)
        Place.objects.create(key=place_key("Northtown", "ND"), name="Northtown", state="ND", lat=48.9, lon=-100.0)
        rows = [
            (1, "START STOP", "MAIN ST", 40.05, -100.0, "3.50"),        # on the line: near route
            (2, "MIDWAY CHEAP", "I-99, EXIT 200", 43.0, -100.02, "2.90"),
            (3, "FAR OFF ROUTE", "I-99", 43.0, -95.0, "1.00"),          # ~250 mi away: outside corridor
            (4, "NORTH STOP", "MAIN ST", 46.5, -100.0, "3.20"),
            (5, "OFF ROUTE NO HWY", "SR-5", 44.0, -100.15, "1.50"),     # ~7 mi off, no highway evidence
            (6, "OFF ROUTE ON HWY", "I-99 EXIT 300", 45.0, -100.15, "2.50"),  # ~7 mi off, but at an I-99 exit
            (7, "OFF ROUTE WRONG HWY", "I-12 EXIT 9", 45.5, -100.15, "1.00"),
        ]
        for opis, name, address, lat, lon, price in rows:
            FuelStation.objects.create(
                opis_id=opis, name=name, address=address, city="X", state="NE", price=Decimal(price), lat=lat, lon=lon
            )
        DatasetVersion.objects.create(pk=1, version="v1", station_count=len(rows))

    def setUp(self):
        cache.clear()
        reset_index()

    def get(self, **params):
        return self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND", **params})



def encode(coords):
    """Minimal polyline encoder ([[lon, lat], ...]) for building test fixtures."""
    out, prev = [], (0, 0)
    for lon, lat in coords:
        cur = (round(lat * 1e5), round(lon * 1e5))
        for d in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(d << 1) if d < 0 else d << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)



def run_concurrently(fn, n=8):
    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results, errors
