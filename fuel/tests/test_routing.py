"""OSRM client: request shape, caching, errors, usage-policy rate limit, memory."""

import json
import pickle
import threading
import time
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from fuel.services import ratelimit, routing
from fuel.services.geo import resolve
from fuel.services.polyline import decode
from fuel.tests.helpers import (
    OSRM_OK,
    NoRateLimitMixin,
    encode,
    nominatim,
    osrm_response,
    run_concurrently,
)


class RoutingClientTests(NoRateLimitMixin, SimpleTestCase):

    @mock.patch("fuel.services.routing._session.get")
    def test_single_request_with_expected_params_then_cached(self, get):
        get.return_value = osrm_response(payload=OSRM_OK)
        route = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(get.call_count, 1)
        url = get.call_args.args[0]
        params = get.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/route/v1/driving/-120.2,38.5;-120.95,40.7"), url)
        self.assertEqual((params["overview"], params["geometries"], params["steps"]), ("false", "polyline", "true"))
        self.assertAlmostEqual(route.distance_miles, 100)
        self.assertEqual(route.coordinates, [[-120.2, 38.5], [-120.95, 40.7]])
        self.assertEqual(route.highways[0][2], {"I5", "US99"})
        self.assertFalse(route.from_cache)

        again = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(get.call_count, 1)
        self.assertTrue(again.from_cache)

    def test_warm_up_opens_the_connection_without_a_routing_call(self):
        with mock.patch("fuel.services.routing._session.head") as head, \
                mock.patch("fuel.services.routing._session.get") as get:
            routing.warm_up_connection()
            for t in threading.enumerate():
                if t.name == "osrm-warmup":
                    t.join(5)
        head.assert_called_once()
        self.assertEqual(head.call_args.args[0], "https://router.project-osrm.org/")
        get.assert_not_called()

    def test_warm_up_failure_is_harmless_and_can_be_disabled(self):
        with mock.patch("fuel.services.routing._session.head", side_effect=requests.ConnectionError("down")) as head:
            routing.warm_up_connection()
            for t in threading.enumerate():
                if t.name == "osrm-warmup":
                    t.join(5)
            head.assert_called_once()  # raised inside the thread, logged, not propagated
        with override_settings(OSRM_WARMUP=False), mock.patch("fuel.services.routing._session.head") as head:
            routing.warm_up_connection()
            head.assert_not_called()

    @mock.patch("fuel.services.routing._session.get")
    def test_cache_is_per_routing_provider(self, get):
        get.return_value = osrm_response(payload=OSRM_OK)
        routing.get_route(38.5, -120.2, 40.7, -120.95)
        with override_settings(OSRM_URL="https://osrm.example.internal"):
            route = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertFalse(route.from_cache)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args.args[0].startswith("https://osrm.example.internal/"))

    @mock.patch("fuel.services.routing._session.get")
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



class OsrmPolicyTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_outbound_call_starts_are_at_least_one_second_apart(self):
        """Timestamps taken at the actual outbound call, with concurrent distinct routes."""
        starts = []

        def record(*_a, **_kw):
            starts.append(time.time())
            return osrm_response(payload=OSRM_OK)

        coords = iter(range(4))
        with mock.patch("fuel.services.routing._session.get", side_effect=record):
            _, errors = run_concurrently(lambda: routing.get_route(30 + next(coords), -120.2, 40.7, -120.95), n=4)
        self.assertEqual(errors, [])
        self.assertEqual(len(starts), 4)
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertGreaterEqual(min(gaps), 1.0, gaps)

    @override_settings(GEOCODE_MAX_WAIT_SECONDS=10)
    def test_geocoder_call_starts_are_at_least_one_second_apart(self):
        starts = []

        def record(*_a, **_kw):
            starts.append(time.time())
            return nominatim(38.9, -77.0)

        n = iter(range(3))
        with mock.patch("fuel.services.geo._session.get", side_effect=record):
            _, errors = run_concurrently(lambda: resolve(f"{next(n)} Main St, Washington, DC"), n=3)
        self.assertEqual(errors, [])
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertGreaterEqual(min(gaps), 1.0, gaps)

    def test_spacing_does_not_depend_on_key_expiry(self):
        """Backends like Memcached may expire a 1-second key early; spacing must still hold."""
        real_add = cache.add

        def add_with_short_expiry(key, value, timeout=None, **kw):
            return real_add(key, value, timeout=0.01 if timeout else timeout, **kw)  # expires almost at once

        starts = []
        with mock.patch.object(cache, "add", side_effect=add_with_short_expiry):
            for _ in range(3):
                ratelimit.acquire("osrm", time.monotonic() + 5)
                starts.append(time.time())
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreaterEqual(min(gaps), 1.0, gaps)

    @override_settings(ROUTING_MAX_WAIT_SECONDS=0.1)
    @mock.patch("fuel.services.routing._session.get")
    def test_saturated_limit_is_503_and_not_cached(self, get):
        get.return_value = osrm_response(payload=OSRM_OK)
        cache.set(ratelimit.last_call_key("osrm"), time.time() + 60, timeout=120)  # next slot is a minute away
        with self.assertRaises(routing.RoutingError) as ctx:
            routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(ctx.exception.status, 503)
        get.assert_not_called()
        cache.delete(ratelimit.last_call_key("osrm"))
        self.assertAlmostEqual(routing.get_route(38.5, -120.2, 40.7, -120.95).distance_miles, 100)  # not a cached failure

    @mock.patch("fuel.services.routing._session.get")
    def test_cache_holds_compact_polyline(self, get):
        long_line = [[-100 + i * 0.001, 40 + i * 0.0005] for i in range(20000)]
        payload = json.loads(json.dumps(OSRM_OK))
        steps = payload["routes"][0]["legs"][0]["steps"]
        steps[0]["geometry"], steps[1]["geometry"] = encode(long_line[:12000]), encode(long_line[11999:])
        get.return_value = osrm_response(payload=payload)
        fresh = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertEqual(fresh.coordinates, decode(encode(long_line)))  # joined, shared point once
        stored = cache.get(routing.cache_key(38.5, -120.2, 40.7, -120.95))
        self.assertTrue(all(isinstance(p, str) for p in stored.polylines))
        self.assertLess(len(pickle.dumps(stored)), len(pickle.dumps(fresh)) / 4)
        again = routing.get_route(38.5, -120.2, 40.7, -120.95)
        self.assertTrue(again.from_cache)
        self.assertEqual(again.coordinates, fresh.coordinates)
