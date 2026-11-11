"""HTTP API and map page end to end (routing mocked)."""

import gzip
import json
import logging
from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache
from django.test import override_settings
from rest_framework.throttling import AnonRateThrottle

from fuel.models import DatasetVersion, FuelStation
from fuel.services import routing
from fuel.services import stations as stations_module
from fuel.services.stations import reset_index
from fuel.tests.helpers import (
    TANK,
    ApiFixture,
    fake_route,
)


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
        self.assertIn("not verified", data["meta"]["assumptions"]["station_location"])
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



class ApiHygieneTests(ApiFixture):
    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_gzip(self, _):
        r = self.client.get("/api/route/", {"start": "Southtown, NE", "finish": "Northtown, ND"}, HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(r["Content-Encoding"], "gzip")
        self.assertIn("fuel_stops", json.loads(gzip.decompress(r.content)))

    def test_validation_errors_share_the_error_shape(self):
        r = self.client.get("/api/route/", {"start": "Southtown, NE"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "Invalid request.")
        self.assertIn("finish", r.json()["details"])

    @mock.patch("fuel.services.routing.get_route", side_effect=routing.RoutingError("busy", status=503))
    def test_503_has_retry_after(self, _):
        r = self.get()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r["Retry-After"], "5")

    @mock.patch.object(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"})
    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_api_and_map_page_are_throttled_per_client(self, _):
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get().status_code, 200)
        r = self.get()
        self.assertEqual(r.status_code, 429)
        self.assertIn("error", r.json())
        self.assertIn("Retry-After", r)
        page = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND"})
        self.assertEqual(page.status_code, 429)
        self.assertIn("Retry-After", page)
        self.assertEqual(self.client.get("/map/").status_code, 200)  # the empty form isn't throttled

    @mock.patch("fuel.services.routing.requests.get", side_effect=requests.ConnectionError("down"))
    def test_failures_are_logged(self, _):
        logger = logging.getLogger("fuel")
        old = logger.level
        logger.setLevel(logging.WARNING)
        try:
            with self.assertLogs("fuel.services.routing", "WARNING") as logs, mock.patch("fuel.services.ratelimit.acquire"):
                with self.assertRaises(routing.RoutingError):
                    routing.get_route(10.5, -120.2, 40.7, -120.95)
        finally:
            logger.setLevel(old)
        self.assertIn("OSRM request failed", logs.output[0])


class FewerStopsAlternativeTests(ApiFixture):
    @override_settings(FEWER_STOPS_SUGGESTION_SAVINGS=1000)
    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_cheapest_plan_reports_a_fewer_stops_option(self, _):
        s = self.get().json()["summary"]
        alt = s["fewer_stops_alternative"]
        self.assertIsNotNone(alt)
        self.assertLess(alt["number_of_stops"], s["number_of_stops"])
        self.assertAlmostEqual(alt["total_fuel_cost"] - s["total_fuel_cost"], alt["extra_cost"], places=1)
        self.assertGreaterEqual(alt["extra_cost"], 0)
        page = self.client.get("/map/", {"start": "Southtown, NE", "finish": "Northtown, ND"})
        self.assertContains(page, "min_stop_savings=1000")

    @override_settings(FEWER_STOPS_SUGGESTION_SAVINGS=5)
    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_no_alternative_when_already_consolidating(self, _):
        self.assertIsNone(self.get(min_stop_savings=5).json()["summary"]["fewer_stops_alternative"])
