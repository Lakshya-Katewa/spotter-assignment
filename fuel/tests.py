from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from fuel.models import FuelStation, Place
from fuel.services import routing
from fuel.services.geo import LocationError, resolve
from fuel.services.names import name_variants, normalize, place_key
import copy

from fuel.services.optimizer import NoFeasiblePlan, consolidate_stops, plan_fuel_stops, simulate
from fuel.services.polyline import decode, simplify
from fuel.services.stations import densify, reset_index


def brute_force_cost(stations, total, rng, mpg, start_fuel):
    """Exact optimum by DP over (station, integer fuel miles). Small inputs only."""
    pts = sorted(stations, key=lambda s: s[1])
    import math

    best = {}  # (index, fuel_miles) -> min cost

    def go(i, fuel):
        key = (i, fuel)
        if key in best:
            return best[key]
        _, here, price = pts[i]
        result = math.inf
        if total - here <= rng:
            need = max(0, total - here - fuel)
            result = need / mpg * price
        for j in range(i + 1, len(pts)):
            dist = pts[j][1] - here
            if dist > rng:
                break
            for buy in range(max(0, dist - fuel), rng - fuel + 1):
                result = min(result, buy / mpg * price + go(j, fuel + buy - dist))
        best[key] = result
        return result

    return min(go(i, start_fuel - pts[i][1]) for i in range(len(pts)) if pts[i][1] <= start_fuel)


class OptimizerTests(SimpleTestCase):
    def test_short_trip_buys_only_what_is_needed(self):
        plan = plan_fuel_stops([("a", 0, 3.0)], total_miles=200, max_range_miles=500, mpg=10)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.total_gallons, 20)
        self.assertAlmostEqual(plan.total_cost, 60)

    def test_buys_just_enough_to_reach_cheaper_station(self):
        stations = [("exp", 0, 4.0), ("cheap", 100, 3.0)]
        plan = plan_fuel_stops(stations, total_miles=400, max_range_miles=500, mpg=10)
        self.assertEqual([s.key for s in plan.stops], ["exp", "cheap"])
        self.assertAlmostEqual(plan.stops[0].gallons, 10)  # 100 miles to the cheaper station
        self.assertAlmostEqual(plan.stops[1].gallons, 30)
        self.assertAlmostEqual(plan.total_cost, 10 * 4 + 30 * 3)

    def test_fills_up_when_current_station_is_cheapest(self):
        stations = [("cheap", 0, 2.0), ("mid", 450, 3.0), ("exp", 600, 5.0)]
        plan = plan_fuel_stops(stations, total_miles=900, max_range_miles=500, mpg=10)
        self.assertAlmostEqual(plan.stops[0].gallons, 50)  # full tank at the cheapest
        self.assertEqual(plan.stops[1].key, "mid")  # cheapest reachable, not the nearest/farthest
        self.assertAlmostEqual(plan.total_gallons, 90)

    def test_gap_longer_than_range_is_reported(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_fuel_stops([("a", 0, 3.0), ("b", 600, 3.0)], total_miles=900, max_range_miles=500, mpg=10)
        self.assertEqual(ctx.exception.gap_start_mile, 0)

    def test_no_station_near_start(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel_stops([("a", 50, 3.0)], total_miles=900, max_range_miles=500, mpg=10, start_fuel_miles=30)

    def test_total_gallons_cover_whole_trip(self):
        stations = [(i, m, p) for i, (m, p) in enumerate([(0, 3.2), (120, 3.0), (300, 3.5), (480, 2.8), (700, 3.1), (950, 3.3)])]
        plan = plan_fuel_stops(stations, total_miles=1200, max_range_miles=500, mpg=10)
        self.assertAlmostEqual(plan.total_gallons, 120)

    def test_empty_start_pays_for_every_mile(self):
        stations = [("near", 5, 3.0), ("nearer_expensive", 2, 4.0), ("mid", 300, 3.1)]
        plan = plan_fuel_stops(stations, total_miles=700, max_range_miles=500, mpg=10, origin_window_miles=30)
        self.assertEqual(plan.stops[0].key, "near")  # cheapest within the window
        self.assertAlmostEqual(plan.total_gallons, 70)  # includes the 5 miles to reach it

    def test_paid_start_fuel_matches_brute_force(self):
        import random

        rnd = random.Random(11)
        for _ in range(25):
            total = rnd.randint(150, 400)
            stations = [(f"s{k}", rnd.randint(1, total - 1), round(rnd.uniform(2.5, 4.0), 2)) for k in range(8)]
            start = rnd.randint(30, 100)
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=100, mpg=10, start_fuel_miles=start)
            except NoFeasiblePlan:
                continue
            first = min(s[1] for s in stations)
            # Brute force from the first station with the leftover starting fuel.
            self.assertAlmostEqual(
                plan.total_cost, brute_force_cost(stations, total, 100, 10, start) if first <= start else 0, places=6
            )

    def test_matches_brute_force_optimum(self):
        import random

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


def assert_feasible(test, plan, total_miles, mpg, tank):
    """Simulate the trip: the tank never runs dry or overflows, and every mile is fuelled."""
    arrivals = simulate(plan.stops, mpg)
    for k, (s, arrival) in enumerate(zip(plan.stops, arrivals)):
        if k:
            test.assertGreaterEqual(arrival, -1e-6, f"ran dry before stop {k}")
        test.assertLessEqual(max(arrival, 0) + s.gallons - (s.mile / mpg if k == 0 else 0), tank + 1e-6, f"overflow at stop {k}")
    last = plan.stops[-1]
    test.assertAlmostEqual(arrivals[-1] + last.gallons, (total_miles - last.mile) / mpg, places=6)


class ConsolidationTests(SimpleTestCase):
    def test_removes_tiny_top_up(self):
        # Like Texarkana -> Hooks: a small top-up 12 miles before a slightly cheaper station.
        stations = [("a", 0, 2.93), ("b", 476, 2.857), ("c", 488, 2.817), ("d", 900, 3.5)]
        plan = plan_fuel_stops(stations, total_miles=950, max_range_miles=500, mpg=10, origin_window_miles=30)
        self.assertEqual([s.key for s in plan.stops], ["a", "b", "c"])
        merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=50, min_stop_savings=5)
        self.assertEqual([s.key for s in merged.stops], ["a", "c"])
        self.assertLess(merged.total_cost - plan.total_cost, 5)
        self.assertAlmostEqual(merged.total_gallons, 95)
        assert_feasible(self, merged, 950, 10, 50)

    def test_zero_threshold_keeps_cheapest_plan(self):
        stations = [("a", 0, 2.93), ("b", 476, 2.857), ("c", 488, 2.817)]
        plan = plan_fuel_stops(stations, total_miles=950, max_range_miles=500, mpg=10, origin_window_miles=30)
        merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=50, min_stop_savings=0)
        self.assertEqual(len(merged.stops), len(plan.stops))

    def test_random_routes_stay_feasible(self):
        import random

        rnd = random.Random(3)
        for _ in range(200):
            total = rnd.randint(300, 3000)
            stations = [(k, rnd.uniform(0, total), round(rnd.uniform(2.7, 4.2), 3)) for k in range(rnd.randint(20, 200))]
            stations.append(("origin", 1.0, 3.5))
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=500, mpg=10, origin_window_miles=30)
            except NoFeasiblePlan:
                continue
            assert_feasible(self, plan, total, 10, 50)
            for threshold in (2, 10, 50):
                merged = consolidate_stops(copy.deepcopy(plan), mpg=10, tank_gallons=50, min_stop_savings=threshold)
                assert_feasible(self, merged, total, 10, 50)
                self.assertGreaterEqual(merged.total_cost, plan.total_cost - 1e-6)
                removed = len(plan.stops) - len(merged.stops)
                self.assertLess(merged.total_cost - plan.total_cost, threshold * max(removed, 1) + 1e-6)


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
        # Example from Google's polyline documentation.
        self.assertEqual(decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]])

    def test_simplify_drops_collinear_points(self):
        self.assertEqual(simplify([[0, 0], [1, 0], [2, 0], [3, 0]]), [[0, 0], [3, 0]])

    def test_densify_spacing_and_length(self):
        pts, cum = densify([[-100.0, 40.0], [-100.0, 41.0]], step_miles=1.0)
        self.assertAlmostEqual(cum[-1], 69.09, places=1)  # 1 degree of latitude
        self.assertLessEqual(max(cum[1:] - cum[:-1]), 1.0 + 1e-9)


def fake_route(*_args, **_kwargs):
    # Straight north-south line from (40N, -100W) to (50N, -100W) ~ 691 miles (router output; endpoints need not match exactly).
    return routing.Route(distance_miles=691.0, duration_seconds=36000, coordinates=[[-100.0, 40.0], [-100.0, 50.0]])


@override_settings(ORIGIN_STATION_WINDOW_MILES=30, STATION_CORRIDOR_MILES=10)
class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.create(key=place_key("Southtown", "NE"), name="Southtown", state="NE", lat=40.0, lon=-100.0)
        Place.objects.create(key=place_key("Northtown", "ND"), name="Northtown", state="ND", lat=48.99, lon=-100.0)
        rows = [
            (1, "START STOP", 40.05, -100.0, "3.50"),
            (2, "MIDWAY CHEAP", 43.0, -100.02, "2.90"),
            (3, "FAR OFF ROUTE", 43.0, -95.0, "1.00"),  # ~250 miles away: must be ignored
            (4, "NORTH STOP", 46.5, -100.0, "3.20"),
        ]
        for opis, name, lat, lon, price in rows:
            FuelStation.objects.create(
                opis_id=opis, name=name, address="I-00", city="X", state="NE", price=Decimal(price), lat=lat, lon=lon
            )

    def setUp(self):
        cache.clear()
        reset_index()

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_plan_endpoint(self, get_route):
        r = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "48.9,-100"})
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        names = [s["name"] for s in data["fuel_stops"]]
        self.assertEqual(names[0], "START STOP")
        self.assertIn("MIDWAY CHEAP", names)
        self.assertNotIn("FAR OFF ROUTE", names)
        self.assertAlmostEqual(data["summary"]["total_gallons"], 69.1, places=1)
        self.assertEqual(data["meta"]["external_calls"]["routing"], 1)
        self.assertIn("/map/?", data["map_url"])

        # Second identical request is served from cache: no routing call.
        r2 = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "48.9,-100"})
        self.assertTrue(r2.json()["meta"]["cached"])
        self.assertEqual(get_route.call_count, 1)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_post_and_no_geometry(self, _):
        r = self.client.post(
            "/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND", "geometry": False}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("geometry", r.json()["route"])

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_min_stop_savings_param(self, _):
        r = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND", "min_stop_savings": 0})
        summary = r.json()["summary"]
        self.assertEqual(summary["extra_cost_for_fewer_stops"], 0)
        self.assertEqual(summary["total_fuel_cost"], summary["cheapest_possible_cost"])
        bad = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND", "min_stop_savings": -1})
        self.assertEqual(bad.status_code, 400)
        page = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND", "min_stop_savings": "nan"})
        self.assertEqual(page.status_code, 400)

    def test_missing_params(self):
        self.assertEqual(self.client.get("/api/route/", {"start": "Southtown, NE"}).status_code, 400)

    def test_rejects_non_us(self):
        r = self.client.get("/api/route/", {"start": "Toronto, ON", "finish": "Southtown, NE"})
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/route/", {"start": "51.5,-0.12", "finish": "Southtown, NE"})
        self.assertEqual(r.status_code, 400)

    @mock.patch("fuel.services.routing.get_route", side_effect=routing.RoutingError("No route found"))
    def test_routing_failure(self, _):
        r = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND"})
        self.assertEqual(r.status_code, 422)

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_map_page(self, _):
        r = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "MIDWAY CHEAP")
        self.assertContains(r, "leaflet")

    def test_local_resolution_needs_no_network(self):
        with mock.patch("fuel.services.geo.requests.get") as get:
            loc = resolve("southtown, nebraska")
            get.assert_not_called()
        self.assertEqual(loc.source, "local")
        with self.assertRaises(LocationError):
            resolve("")
