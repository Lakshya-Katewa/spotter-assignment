"""Cheapest-plan optimizer and optional stop consolidation."""

import copy
import random

from django.test import SimpleTestCase

from fuel.services.optimizer import (
    NoFeasiblePlan,
    consolidate_stops,
    plan_fuel_stops,
)
from fuel.tests.helpers import (
    TANK,
    assert_feasible,
    brute_force_cost,
)


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

    def test_pay_all_fuel_bills_fuel_to_first_station(self):
        plan = plan_fuel_stops([("a", 20, 3.0), ("b", 300, 2.0)], total_miles=600, max_range_miles=500, mpg=10, approach_miles=25)
        self.assertAlmostEqual(plan.start_fuel_gallons, 2)
        self.assertAlmostEqual(plan.start_fuel_cost, 6.0)  # 2 gal at a's $3.00
        self.assertAlmostEqual(plan.start_fuel_gallons + plan.total_gallons, 60)  # every gallon burned is paid for
        self.assertAlmostEqual(plan.total_cost, 6.0 + 28 * 3.0 + 30 * 2.0)
        assert_feasible(self, plan, 600)

    def test_pay_all_fuel_short_trip_still_buys(self):
        plan = plan_fuel_stops([("a", 0, 3.0)], total_miles=100, max_range_miles=500, mpg=10)
        self.assertEqual([s.key for s in plan.stops], ["a"])
        self.assertAlmostEqual(plan.total_cost, 30.0)

    def test_pay_all_fuel_skips_pricey_stations_near_the_start(self):
        # Regression: pinning the first stop to the first station on the route gave a chain
        # of tiny top-ups (mile 0 at $3.57, then 0.1 gal at mile 12, ...).
        stations = [("pricey", 0, 3.6), ("mid", 12, 3.4), ("cheap", 25, 3.0), ("far", 400, 3.1)]
        plan = plan_fuel_stops(stations, total_miles=800, max_range_miles=500, mpg=10)
        self.assertEqual(plan.stops[0].key, "cheap")
        self.assertAlmostEqual(plan.start_fuel_gallons, 2.5)
        self.assertAlmostEqual(plan.start_fuel_cost, 2.5 * 3.0)
        self.assertNotIn("pricey", [s.key for s in plan.stops])
        assert_feasible(self, plan, 800)
        plan = plan_fuel_stops(stations, total_miles=800, max_range_miles=500, mpg=10, approach_miles=5)
        self.assertEqual(plan.stops[0].key, "pricey")

    def test_pay_all_fuel_far_first_station(self):
        # No station within the approach distance (Houston in the price file): the first
        # station on the route is the first stop, reached on billed starting fuel.
        plan = plan_fuel_stops([("far", 106, 3.0), ("next", 400, 2.9)], total_miles=900, max_range_miles=500, mpg=10)
        self.assertEqual(plan.stops[0].key, "far")
        self.assertAlmostEqual(plan.start_fuel_gallons, 10.6)
        self.assertAlmostEqual(plan.start_fuel_cost, 10.6 * 3.0)
        assert_feasible(self, plan, 900)
        # Never more than a full tank's worth.
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_fuel_stops([("far", 550, 3.0)], total_miles=900, max_range_miles=500, mpg=10)
        self.assertEqual(ctx.exception.code, "no_station_near_start")
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_fuel_stops([], total_miles=300, max_range_miles=500, mpg=10)
        self.assertEqual(ctx.exception.code, "no_stations_on_route")

    def test_pay_all_fuel_station_only_at_destination(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel_stops([("end", 100, 3.0)], total_miles=100, max_range_miles=500, mpg=10)

    def test_pay_all_fuel_without_stations(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_fuel_stops([], total_miles=300, max_range_miles=500, mpg=10)
        self.assertIn("no fuel station along this route", str(ctx.exception))

    def test_pay_all_fuel_matches_brute_force(self):
        rnd, checked = random.Random(11), 0
        for _ in range(150):
            total = rnd.randint(50, 400)
            stations = [(k, rnd.randint(0, total), round(rnd.uniform(2.5, 4.5), 2)) for k in range(rnd.randint(1, 9))]
            approach = rnd.choice((5, 15, 30))
            try:
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=100, mpg=10, approach_miles=approach)
            except NoFeasiblePlan:
                continue
            # Model: pick any first station before the destination within the approach
            # distance (or the first one on the route), and pay for the drive there at its price.
            miles = sorted({m for _, m, _ in stations if m < total})
            options = []
            for m in (m for m in miles if m <= max(approach, miles[0])):
                price = min(p for _, mm, p in stations if mm == m)
                try:
                    rest = brute_force_cost([s for s in stations if s[1] >= m], total, 100, 10, m)
                except ValueError:
                    continue
                options.append(m / 10 * price + rest)
            self.assertAlmostEqual(plan.total_cost, min(options), places=6)
            assert_feasible(self, plan, total, tank=10)
            checked += 1
        self.assertGreater(checked, 30)

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
                plan = plan_fuel_stops(stations, total_miles=total, max_range_miles=100, mpg=10, approach_miles=0)
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
