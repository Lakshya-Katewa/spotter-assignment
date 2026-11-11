"""Concurrent cold-cache coalescing and malformed upstream responses."""

import time
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from fuel.services import ratelimit, routing, singleflight
from fuel.services.geo import GeocoderUnavailable, resolve
from fuel.tests.helpers import (
    OSRM_OK,
    NoRateLimitMixin,
    encode,
    nominatim,
    osrm_response,
    run_concurrently,
)


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
        ratelimit._locks["nominatim"].acquire()  # another thread is stuck holding the limiter
        try:
            t0 = time.monotonic()
            with self.assertRaises(GeocoderUnavailable):
                resolve("5 Fifth St, Washington, DC")
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            ratelimit._locks["nominatim"].release()



class MalformedUpstreamTests(NoRateLimitMixin, SimpleTestCase):

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
            with self.subTest(name), mock.patch("fuel.services.geo.requests.get", return_value=r):
                with self.assertRaises(GeocoderUnavailable) as ctx:
                    resolve(f"{i} Main St, Springfield, IL")
                self.assertEqual(ctx.exception.status, 502)
