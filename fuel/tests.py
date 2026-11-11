import copy
import math
import random
import time
from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from fuel.models import DatasetVersion, FuelStation, Place
from fuel.services import geo, highways, routing
from fuel.services.boundary import contains
from fuel.services.geo import GeocoderUnavailable, LocationError, resolve
from fuel.services.names import name_variants, normalize, place_key
from fuel.services.optimizer import NoFeasiblePlan, consolidate_stops, plan_fuel_stops, simulate
from fuel.services.polyline import decode, simplify
from fuel.services.stations import densify, reset_index

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


# -- optimizer ---------------------------------------------------------------
class OptimizerTests(SimpleTestCase):
    def test_short_trip_buys_only_what_is_needed(self):
        plan = plan_fuel_stops([("a", 0, 3.0)], total_miles=200, max_range_miles=500, mpg=10)
        self.assertAlmostEqual(plan.total_gallons, 20)
        self.assertAlmostEqual(plan.total_cost, 60)

    def test_buys_just_enough_to_reach_cheaper_station(self):
        plan = plan_fuel_stops([("exp", 0, 4.0), ("cheap", 100, 3.0)], total_miles=400, max_range_miles=500, mpg=10)
        self.assertEqual([(s.key, round(s.gallons, 6)) for s in plan.stops], [("exp", 10), ("cheap", 30)])

    def test_fills_up_when_current_station_is_cheapest(self):
        stations = [("cheap", 0, 2.0), ("mid", 450, 3.0), ("exp", 600, 5.0)]
        plan = plan_fuel_stops(stations, total_miles=900, max_range_miles=500, mpg=10)
        self.assertAlmostEqual(plan.stops[0].gallons, 50)
        self.assertEqual(plan.stops[1].key, "mid")

    def test_gap_longer_than_range_is_reported(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_fuel_stops([("a", 0, 3.0), ("b", 600, 3.0)], total_miles=900, max_range_miles=500, mpg=10)
        self.assertEqual(ctx.exception.gap_start_mile, 0)

    def test_no_station_reachable_on_start_fuel(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel_stops([("a", 50, 3.0)], total_miles=900, max_range_miles=500, mpg=10, start_fuel_gallons=3)

    def test_zero_distance_trip_needs_no_stations(self):
        for start in (0, 3):
            plan = plan_fuel_stops([], total_miles=0, max_range_miles=500, mpg=10, start_fuel_gallons=start)
            self.assertEqual((plan.stops, plan.total_cost, plan.total_gallons), ([], 0.0, 0.0))
            self.assertAlmostEqual(plan.end_fuel_gallons, start)

    def test_regression_no_purchase_exceeds_tank(self):
        # Previously the 30 miles driven to the first pump were billed there on top of a
        # full tank: a 53-gallon "purchase" for a 50-gallon tank.
        plan = plan_fuel_stops([("a", 30, 2.0), ("b", 520, 3.0)], total_miles=1000, max_range_miles=500, mpg=10, start_fuel_gallons=3)
        self.assertAlmostEqual(plan.start_fuel_gallons, 3)
        self.assertAlmostEqual(plan.stops[0].gallons, 50)
        self.assertAlmostEqual(plan.total_gallons, 97)  # bought; 100 consumed, 3 came from the tank
        assert_feasible(self, plan, 1000)

    def test_start_fuel_is_fixed_not_derived_from_the_plan(self):
        # Two different station sets, same starting fuel: costs are comparable.
        for stations in ([("near", 5, 3.0), ("mid", 300, 3.1)], [("far", 25, 2.5), ("mid", 300, 3.1)]):
            plan = plan_fuel_stops(stations, total_miles=700, max_range_miles=500, mpg=10, start_fuel_gallons=3)
            self.assertEqual(plan.start_fuel_gallons, 3)
            assert_feasible(self, plan, 700)

    def test_given_start_fuel_is_tracked(self):
        plan = plan_fuel_stops([("a", 100, 3.0), ("b", 400, 2.5)], total_miles=700, max_range_miles=500, mpg=10, start_fuel_gallons=20)
        self.assertAlmostEqual(plan.start_fuel_gallons, 20)
        self.assertAlmostEqual(plan.end_fuel_gallons, 0)
        assert_feasible(self, plan, 700)
        enough = plan_fuel_stops([("a", 100, 3.0)], total_miles=300, max_range_miles=500, mpg=10, start_fuel_gallons=40)
        self.assertEqual(enough.stops, [])
        self.assertAlmostEqual(enough.end_fuel_gallons, 10)

    def test_start_fuel_validated(self):
        with self.assertRaises(ValueError):
            plan_fuel_stops([("a", 0, 3.0)], total_miles=100, max_range_miles=500, mpg=10, start_fuel_gallons=51)

    def test_given_start_fuel_matches_brute_force(self):
        rnd = random.Random(11)
        for _ in range(25):
            total = rnd.randint(150, 400)
            stations = [(f"s{k}", rnd.randint(1, total - 1), round(rnd.uniform(2.5, 4.0), 2)) for k in range(8)]
            start = rnd.randint(30, 100)
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=100, mpg=10, start_fuel_gallons=start / 10)
            except NoFeasiblePlan:
                continue
            self.assertAlmostEqual(plan.total_cost, brute_force_cost(stations, total, 100, 10, start), places=6)

    def test_matches_brute_force_optimum(self):
        rnd = random.Random(7)
        for _ in range(25):
            total = rnd.randint(150, 400)
            stations = [("s0", 0, round(rnd.uniform(2.5, 4.0), 2))]
            stations += [(f"s{k}", rnd.randint(1, total - 1), round(rnd.uniform(2.5, 4.0), 2)) for k in range(1, 8)]
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=100, mpg=10)
            except NoFeasiblePlan:
                continue
            self.assertAlmostEqual(plan.total_cost, brute_force_cost(stations, total, 100, 10, 0), places=6)


class ConsolidationTests(SimpleTestCase):
    def test_removes_tiny_top_up(self):
        # Like Texarkana -> Hooks: a small top-up 12 miles before a slightly cheaper station.
        stations = [("a", 0, 2.93), ("b", 476, 2.857), ("c", 488, 2.817), ("d", 900, 3.5)]
        plan = plan_fuel_stops(stations, total_miles=950, max_range_miles=500, mpg=10)
        self.assertEqual([s.key for s in plan.stops], ["a", "b", "c"])
        merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=TANK, min_stop_savings=5)
        self.assertEqual([s.key for s in merged.stops], ["a", "c"])
        self.assertLess(merged.total_cost - plan.total_cost, 5)
        assert_feasible(self, merged, 950)

    def test_zero_threshold_keeps_cheapest_plan(self):
        stations = [("a", 0, 2.93), ("b", 476, 2.857), ("c", 488, 2.817)]
        plan = plan_fuel_stops(stations, total_miles=950, max_range_miles=500, mpg=10)
        merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=TANK, min_stop_savings=0)
        self.assertEqual(len(merged.stops), len(plan.stops))

    def test_random_routes_stay_feasible(self):
        rnd = random.Random(3)
        for n in range(200):
            total = rnd.randint(300, 3000)
            stations = [(k, rnd.uniform(0, total), round(rnd.uniform(2.7, 4.2), 3)) for k in range(rnd.randint(20, 200))]
            stations.append(("origin", 1.0, 3.5))
            start = 3 if n % 2 else rnd.uniform(0.2, TANK)
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=500, mpg=10, start_fuel_gallons=start)
            except NoFeasiblePlan:
                continue
            assert_feasible(self, plan, total)
            for threshold in (2, 10, 50):
                merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=TANK, min_stop_savings=threshold)
                assert_feasible(self, merged, total)
                self.assertGreaterEqual(merged.total_cost, plan.total_cost - 1e-6)
                removed = len(plan.stops) - len(merged.stops)
                self.assertLess(merged.total_cost - plan.total_cost, threshold * max(removed, 1) + 1e-6)


# -- pure helpers ------------------------------------------------------------
class NameTests(SimpleTestCase):
    def test_variants_strip_one_descriptor_at_a_time(self):
        self.assertIn(normalize("Bay City"), name_variants("Bay City city"))
        self.assertIn(normalize("Nashville Davidson"), name_variants("Nashville-Davidson metropolitan government (balance)"))
        self.assertIn("athens", name_variants("Athens-Clarke County unified government (balance)"))

    def test_place_key_normalizes(self):
        self.assertEqual(place_key("St. Louis", "mo"), place_key("Saint Louis", "MO"))
        self.assertEqual(place_key("Ft Worth", "TX"), place_key("Fort Worth", "TX"))
        self.assertEqual(place_key("Mc Alpin", "FL"), place_key("McAlpin", "FL"))


class GeometryTests(SimpleTestCase):
    def test_polyline_decode(self):
        self.assertEqual(decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]])

    def test_polyline_rejects_truncated_input(self):
        for bad in ("_p~iF~ps|U_ul", "_p~iF", "\x00"):
            with self.assertRaises(ValueError):
                decode(bad)

    def test_simplify_drops_collinear_points(self):
        self.assertEqual(simplify([[0, 0], [1, 0], [2, 0], [3, 0]]), [[0, 0], [3, 0]])

    def test_densify_spacing_and_length(self):
        pts, cum = densify([[-100.0, 40.0], [-100.0, 41.0]], step_miles=1.0)
        self.assertAlmostEqual(cum[-1], 69.09, places=1)
        self.assertLessEqual(max(cum[1:] - cum[:-1]), 1.0 + 1e-9)


class HighwayTests(SimpleTestCase):
    def test_address_refs(self):
        self.assertEqual(highways.from_address("I-44, EXIT 283 & US-69"), {"I44", "US69"})
        self.assertEqual(highways.from_address("US-46/US-1/US-9"), {"US46", "US1", "US9"})
        self.assertEqual(highways.from_address("IH-35 EXIT 4"), {"I35"})
        self.assertEqual(highways.from_address("SR- 60 & FL TURNPIKE, EXIT 193"), set())

    def test_osrm_refs_and_spans(self):
        self.assertEqual(highways.from_osrm_ref("I 30; US 70"), {"I30", "US70"})
        spans = highways.spans_from_steps([{"distance": 1609.344 * 10, "ref": "I 5"}, {"distance": 1609.344 * 5, "ref": "US 99"}], 1609.344)
        self.assertEqual(highways.refs_near(spans, 3, 1), {"I5"})
        self.assertEqual(highways.refs_near(spans, 14, 1), {"US99"})


class BoundaryTests(SimpleTestCase):
    def test_points_on_both_sides_of_the_borders(self):
        inside = {"Detroit": (42.3314, -83.0458), "San Ysidro": (32.555, -117.045), "El Paso": (31.7619, -106.485),
                  "Honolulu": (21.3069, -157.8583), "Anchorage": (61.2181, -149.9003), "Key West": (24.5551, -81.78)}
        outside = {"Toronto": (43.6532, -79.3832), "Windsor ON": (42.3149, -83.0364), "Tijuana": (32.5149, -117.0382),
                   "Juarez": (31.6904, -106.4245), "Vancouver": (49.2827, -123.1207), "Atlantic": (39.5, -72.0)}
        for name, (lat, lon) in inside.items():
            self.assertTrue(contains(lat, lon), name)
        for name, (lat, lon) in outside.items():
            self.assertFalse(contains(lat, lon), name)


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


class RoutingClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @mock.patch("fuel.services.routing.requests.get")
    def test_single_request_with_expected_params_then_cached(self, get):
        get.return_value = osrm_response(payload=OSRM_OK)
        route = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(get.call_count, 1)
        url = get.call_args.args[0]
        params = get.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/route/v1/driving/-120.2,38.5;-120.95,40.7"), url)
        self.assertEqual((params["overview"], params["geometries"], params["steps"]), ("full", "polyline", "true"))
        self.assertAlmostEqual(route.distance_miles, 100)
        self.assertEqual(route.coordinates, [[-120.2, 38.5], [-120.95, 40.7]])
        self.assertEqual(route.highways[0][2], {"I5", "US99"})
        self.assertFalse(route.from_cache)

        again = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(get.call_count, 1)
        self.assertTrue(again.from_cache)

    @mock.patch("fuel.services.routing.requests.get")
    def test_upstream_failures(self, get):
        cases = [
            (osrm_response(400, {"code": "NoRoute", "message": "Impossible route"}), 422),
            (osrm_response(500, {"code": "InternalError"}), 502),
            (osrm_response(200, {"code": "Ok", "routes": []}), 502),
            (osrm_response(502, json_error=True), 502),
            (requests.ConnectionError("down"), 502),
            (requests.Timeout("slow"), 502),
        ]
        for i, (effect, status) in enumerate(cases):
            get.reset_mock()
            get.side_effect = effect if isinstance(effect, Exception) else None
            get.return_value = None if isinstance(effect, Exception) else effect
            with self.assertRaises(routing.RoutingError) as ctx:
                routing.get_route(38.5 + i, -120.2, 40.7, -120.95)
            self.assertEqual(ctx.exception.status, status, i)
            self.assertEqual(get.call_count, 1)


# -- geocoding ---------------------------------------------------------------
def nominatim(lat, lon, label="Somewhere, USA"):
    r = mock.Mock(status_code=200)
    r.raise_for_status.return_value = None
    r.json.return_value = [{"lat": str(lat), "lon": str(lon), "display_name": label}] if lat is not None else []
    return r


@override_settings(GEOCODE_MAX_WAIT_SECONDS=3)
class GeocodingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.create(key=place_key("Washington", "DC"), name="Washington", state="DC", lat=38.904, lon=-77.016)

    def setUp(self):
        cache.clear()

    @mock.patch("fuel.services.geo.requests.get")
    def test_city_state_is_local(self, get):
        for q in ["Washington, DC", "washington, District of Columbia", "Washington, DC, USA", "Washington, DC 20500"]:
            self.assertEqual(resolve(q).source, "local", q)
        get.assert_not_called()

    @mock.patch("fuel.services.geo.requests.get")
    def test_street_address_is_geocoded_not_collapsed_to_city(self, get):
        get.return_value = nominatim(38.8977, -77.0365, "White House")
        loc = resolve("1600 Pennsylvania Ave NW, Washington, DC")
        self.assertEqual(loc.source, "nominatim")
        self.assertAlmostEqual(loc.lat, 38.8977)
        self.assertEqual(get.call_count, 1)

    @mock.patch("fuel.services.geo.requests.get")
    def test_results_are_cached_including_misses(self, get):
        get.return_value = nominatim(38.8977, -77.0365)
        resolve("1600 Pennsylvania Ave NW, Washington, DC")
        self.assertEqual(resolve("1600  pennsylvania ave nw, washington, dc").source, "nominatim-cache")
        get.return_value = nominatim(None, None)
        for _ in range(2):
            with self.assertRaises(LocationError):
                resolve("Nowhere Street 1, Atlantis")
        self.assertEqual(get.call_count, 2)

    @mock.patch("fuel.services.geo.requests.get")
    def test_rate_limited_to_one_request_per_second(self, get):
        get.return_value = nominatim(38.8977, -77.0365)
        t0 = time.monotonic()
        resolve("1 First St, Washington, DC")
        resolve("2 Second St, Washington, DC")
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertEqual(get.call_count, 2)

    @override_settings(GEOCODE_MAX_WAIT_SECONDS=0.1)
    @mock.patch("fuel.services.geo.requests.get")
    def test_busy_limiter_returns_503_instead_of_exceeding_rate(self, get):
        cache.add(geo._RATE_KEY, 1, timeout=60)
        with self.assertRaises(GeocoderUnavailable) as ctx:
            resolve("3 Third St, Washington, DC")
        self.assertEqual(ctx.exception.status, 503)
        get.assert_not_called()

    @mock.patch("fuel.services.geo.requests.get", side_effect=requests.ConnectionError("down"))
    def test_geocoder_outage_is_503(self, _):
        r = self.client.get("/api/route/", {"start": "4 Fourth St, Washington, DC", "finish": "Washington, DC"})
        self.assertEqual(r.status_code, 503)

    @mock.patch("fuel.services.geo.requests.get")
    def test_foreign_results_rejected(self, get):
        with self.assertRaises(LocationError):
            resolve("43.6532,-79.3832")  # Toronto: inside the old bounding box
        get.return_value = nominatim(42.3149, -83.0364)  # a geocoder hit in Windsor, ON
        with self.assertRaises(LocationError):
            resolve("Some Street, Windsor")


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


class ApiTests(ApiFixture):

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_plan_endpoint(self, get_route):
        r = self.get()
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        stops = {s["name"]: s for s in data["fuel_stops"]}
        self.assertEqual(list(stops)[0], "START STOP")
        self.assertEqual(stops["START STOP"]["route_match"], "city centre within 2 mi of the route")
        self.assertEqual(stops["MIDWAY CHEAP"]["route_match"], "address names I99, which the route uses nearby")
        self.assertIn("OFF ROUTE ON HWY", stops)
        for rejected in ("FAR OFF ROUTE", "OFF ROUTE NO HWY", "OFF ROUTE WRONG HWY"):
            self.assertNotIn(rejected, stops)

        s = data["summary"]
        self.assertAlmostEqual(s["fuel_consumed_gallons"], 69.1, places=1)
        self.assertAlmostEqual(s["starting_fuel_gallons"] + s["fuel_purchased_gallons"] - s["fuel_left_at_arrival_gallons"],
                               s["fuel_consumed_gallons"], places=1)
        for stop in data["fuel_stops"]:
            self.assertLessEqual(stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"], TANK + 0.01)
        self.assertTrue(s["is_estimate"])
        self.assertEqual(s["starting_fuel_gallons"], 3)  # fixed default
        self.assertIn("not verification", data["meta"]["assumptions"]["station_location"])
        self.assertEqual(data["meta"]["external_calls"], {"routing": 1, "geocoding": 0})

        r2 = self.get()
        self.assertTrue(r2.json()["meta"]["cached"])
        self.assertEqual(r2.json()["meta"]["external_calls"]["routing"], 0)
        self.assertEqual(get_route.call_count, 1)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_default_is_strictly_cheapest(self, _):
        s = self.get().json()["summary"]
        self.assertEqual(s["total_fuel_cost"], s["cheapest_possible_cost"])
        self.assertEqual(s["extra_cost_for_fewer_stops"], 0)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_start_fuel_param(self, _):
        s = self.get(start_fuel_gallons=50).json()["summary"]
        self.assertEqual(s["starting_fuel_gallons"], 50)
        self.assertAlmostEqual(s["fuel_purchased_gallons"], 69.1 - 50, places=1)
        self.assertEqual(self.get(start_fuel_gallons=51).status_code, 400)
        self.assertEqual(self.get(start_fuel_gallons=-1).status_code, 400)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_params_validated(self, _):
        self.assertEqual(self.get(min_stop_savings=-1).status_code, 400)
        page = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND", "min_stop_savings": "nan"})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(self.client.get("/api/route/", {"start": "Southtown, NE"}).status_code, 400)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_post_without_geometry(self, _):
        r = self.client.post("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND", "geometry": False},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("geometry", r.json()["route"])

    def test_rejects_non_us(self):
        self.assertEqual(self.client.get("/api/route/", {"start": "Toronto, ON", "finish": "Southtown, NE"}).status_code, 400)
        self.assertEqual(self.client.get("/api/route/", {"start": "43.65,-79.38", "finish": "Southtown, NE"}).status_code, 400)

    def test_routing_errors_map_to_status(self):
        for status in (422, 502):
            with mock.patch("fuel.services.routing.get_route", side_effect=routing.RoutingError("x", status=status)):
                cache.clear()
                self.assertEqual(self.get().status_code, status)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_new_dataset_version_invalidates_index_and_plan_cache(self, _):
        before = self.get().json()
        FuelStation.objects.filter(opis_id=2).update(price=Decimal("1.00"))
        self.assertEqual(self.get().json()["summary"], before["summary"])  # same version: cached

        DatasetVersion.objects.filter(pk=1).update(version="v2")
        after = self.get().json()
        self.assertFalse(after["meta"]["cached"])
        self.assertEqual(after["meta"]["station_dataset_version"], "v2")
        self.assertLess(after["summary"]["total_fuel_cost"], before["summary"]["total_fuel_cost"])

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_map_page_escapes_data(self, _):
        evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'
        FuelStation.objects.filter(opis_id=1).update(name=evil)
        r = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn('id="plan-data"', html)
        self.assertNotIn("innerHTML", html)


# -- review round 2: concurrency, malformed upstream data, snapshots ---------
import threading  # noqa: E402

from fuel.services import singleflight, stations as stations_module  # noqa: E402


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


class ConcurrencyTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_concurrent_cold_cache_routes_make_one_upstream_call(self):
        calls = []

        def slow_osrm(*_a, **_kw):
            calls.append(1)
            time.sleep(0.3)
            return osrm_response(payload=OSRM_OK)

        with mock.patch("fuel.services.routing.requests.get", side_effect=slow_osrm):
            results, errors = run_concurrently(lambda: routing.get_route(38.5, -120.2, 40.7, -120.95))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(not r.from_cache for r in results), 1)

    def test_upstream_failure_is_cached_briefly_no_stampede(self):
        def slow_failure(*_a, **_kw):
            time.sleep(0.3)  # all 8 requests arrive while the first call is still failing
            raise requests.ConnectionError("down")

        with mock.patch("fuel.services.routing.requests.get", side_effect=slow_failure) as get:
            _, errors = run_concurrently(lambda: routing.get_route(38.5, -120.2, 40.7, -120.95))
        self.assertEqual(len(errors), 8)
        self.assertTrue(all(isinstance(e, routing.RoutingError) and e.status == 502 for e in errors))
        self.assertEqual(get.call_count, 1)

    def test_singleflight_waiter_gives_up_with_busy(self):
        cache.add("lock:k", "someone-else", timeout=60)  # another process holds the lock
        with self.assertRaises(singleflight.Busy):
            singleflight.get_or_compute("k", lambda: 1, ttl=60, wait_seconds=0.2)

    @override_settings(GEOCODE_MAX_WAIT_SECONDS=3)
    def test_concurrent_identical_geocodes_make_one_call(self):
        def slow_nominatim(*_a, **_kw):
            time.sleep(0.2)
            return nominatim(38.8977, -77.0365)

        with mock.patch("fuel.services.geo.requests.get", side_effect=slow_nominatim) as get:
            results, errors = run_concurrently(lambda: resolve("1600 Pennsylvania Ave NW, Washington, DC"))
        self.assertEqual(errors, [])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(sorted({r.source for r in results}), ["nominatim", "nominatim-cache"])

    @override_settings(GEOCODE_MAX_WAIT_SECONDS=0.2)
    def test_limiter_wait_is_bounded_even_behind_the_lock(self):
        geo._rate_lock.acquire()  # another thread is stuck holding the limiter
        try:
            t0 = time.monotonic()
            with self.assertRaises(GeocoderUnavailable):
                resolve("5 Fifth St, Washington, DC")
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            geo._rate_lock.release()


class MalformedUpstreamTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_malformed_osrm_responses_become_502(self):
        good = OSRM_OK["routes"][0]
        bad_routes = {
            "empty route object": {},
            "truncated polyline": {**good, "geometry": "_p~iF~ps|U_ul"},
            "invalid polyline chars": {**good, "geometry": "\x01\x02"},
            "geometry not a string": {**good, "geometry": [[0, 0]]},
            "negative distance": {**good, "distance": -5},
            "string distance": {**good, "distance": "abc"},
            "infinite duration": {**good, "duration": float("inf")},
            "boolean distance": {**good, "distance": True},
            "single point but non-zero distance": {**good, "geometry": "_p~iF~ps|U"},
            "coordinates out of range": {**good, "geometry": encode([[10.0, 95.0], [10.0, 96.0]])},
        }
        for i, (name, route) in enumerate(bad_routes.items()):
            with self.subTest(name), mock.patch(
                "fuel.services.routing.requests.get", return_value=osrm_response(payload={"code": "Ok", "routes": [route]})
            ):
                with self.assertRaises(routing.RoutingError) as ctx:
                    routing.get_route(30 + i, -100, 31, -100)
                self.assertEqual(ctx.exception.status, 502)
        for i, payload in enumerate([[1, 2], "text", None, {"code": "Ok"}, {"code": "Ok", "routes": "x"}, {"code": "Ok", "routes": [3]}]):
            with self.subTest(payload=payload), mock.patch(
                "fuel.services.routing.requests.get", return_value=osrm_response(payload=payload)
            ):
                with self.assertRaises(routing.RoutingError):
                    routing.get_route(45 + i, -100, 46, -100)

    def test_malformed_steps_are_ignored_not_fatal(self):
        route = {**OSRM_OK["routes"][0], "legs": [{"steps": [{"distance": "x", "ref": "I 5"}, None, {"distance": 1609.344, "ref": 7}]}, "junk"]}
        r = routing.parse_osrm(200, {"code": "Ok", "routes": [route]})
        self.assertEqual(r.highways, [])

    @override_settings(GEOCODE_MAX_WAIT_SECONDS=3)
    def test_malformed_nominatim_responses_become_502(self):
        payloads = {"[{}]": [{}], "bad lat": [{"lat": "x", "lon": "1"}], "dict body": {"a": 1},
                    "list of str": ["x"], "lat out of range": [{"lat": "99", "lon": "1"}], "nan": [{"lat": "nan", "lon": "1"}]}
        for i, (name, payload) in enumerate(payloads.items()):
            r = mock.Mock(status_code=200)
            r.raise_for_status.return_value = None
            r.json.return_value = payload
            with self.subTest(name), mock.patch("fuel.services.geo.requests.get", return_value=r), \
                    mock.patch("fuel.services.geo._acquire_rate_slot"):
                with self.assertRaises(GeocoderUnavailable) as ctx:
                    resolve(f"{i} Main St, Springfield, IL")
                self.assertEqual(ctx.exception.status, 502)


class SnapshotTests(ApiFixture):
    def test_reload_during_request_uses_one_consistent_snapshot(self):
        def route_then_reload(*a, **kw):
            # A reload lands mid-request: every station is replaced and prices change.
            FuelStation.objects.all().delete()
            DatasetVersion.objects.filter(pk=1).update(version="v-new")
            return fake_route()

        with mock.patch("fuel.services.routing.get_route", side_effect=route_then_reload):
            r = self.get()
        self.assertEqual(r.status_code, 200, r.content)  # previously a KeyError -> 500
        data = r.json()
        self.assertEqual(data["meta"]["station_dataset_version"], "v1")
        self.assertEqual(data["fuel_stops"][0]["name"], "START STOP")
        self.assertEqual(data["fuel_stops"][0]["price_per_gallon"], 3.5)

    def test_snapshot_retries_while_version_moves(self):
        versions = iter(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"])
        with mock.patch.object(DatasetVersion, "current", side_effect=lambda: next(versions)):
            reset_index()
            self.assertEqual(self.get().status_code, 503)

    def test_snapshot_reads_matching_version(self):
        seq = iter(["v1", "v0", "v1", "v1", "v1", "v1"])  # changes once during the first read
        with mock.patch.object(DatasetVersion, "current", side_effect=lambda: next(seq)):
            version, rows = stations_module._read_snapshot()
        self.assertEqual(version, "v1")
        self.assertEqual(len(rows), 7)

    @mock.patch("fuel.services.routing.get_route")
    def test_zero_distance_trip(self, get_route):
        get_route.return_value = routing.Route(distance_miles=0.0, duration_seconds=0, coordinates=[[-100.0, 40.0], [-100.0, 40.0]], highways=[])
        FuelStation.objects.all().delete()
        DatasetVersion.objects.filter(pk=1).update(version="empty")
        r = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Southtown, NE"})
        self.assertEqual(r.status_code, 200, r.content)
        s = r.json()["summary"]
        self.assertEqual((s["total_fuel_cost"], s["number_of_stops"]), (0, 0))
