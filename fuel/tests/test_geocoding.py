"""Location resolution: local Census table, Nominatim cache/limit, offshore centroids."""

import time
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.models import Place
from fuel.services import ratelimit
from fuel.services.geo import GeocoderUnavailable, LocationError, resolve
from fuel.services.names import place_key
from fuel.tests.helpers import (
    nominatim,
)


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
        cache.set(ratelimit.last_call_key("nominatim"), time.time() + 60, timeout=120)  # next slot is a minute away
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



# -- review round 4: offshore Census points, polyline bounds, simplification -
class OffshorePlaceTests(TestCase):
    """Real US places whose Census internal point is not a usable location."""

    @classmethod
    def setUpTestData(cls):
        # Actual Gazetteer internal points.
        Place.objects.create(key=place_key("San Francisco", "CA"), name="San Francisco city", state="CA", lat=37.727239, lon=-123.032229)
        Place.objects.create(key=place_key("Honolulu", "HI"), name="Honolulu CCD", state="HI", lat=27.793, lon=-175.848, rank=2)
        Place.objects.create(key=place_key("Miami", "FL"), name="Miami city", state="FL", lat=25.775, lon=-80.209)

    def setUp(self):
        cache.clear()

    @mock.patch("fuel.services.ratelimit.acquire")
    @mock.patch("fuel.services.geo.requests.get")
    def test_offshore_centroid_falls_back_to_geocoding_the_city(self, get, _):
        cases = {"San Francisco, CA": (37.7793, -122.4193), "Honolulu, HI": (21.3069, -157.8583)}
        for query, (lat, lon) in cases.items():
            get.return_value = nominatim(lat, lon, query)
            loc = resolve(query)
            self.assertEqual(loc.source, "nominatim", query)
            self.assertAlmostEqual(loc.lat, lat)
            self.assertEqual(get.call_args.kwargs["params"]["q"], query)
        self.assertEqual(resolve("San Francisco, CA").source, "nominatim-cache")  # one lookup per city

    @mock.patch("fuel.services.geo.requests.get")
    def test_onshore_centroid_stays_local(self, get):
        self.assertEqual(resolve("Miami, FL").source, "local")
        get.assert_not_called()

    @mock.patch("fuel.services.ratelimit.acquire")
    @mock.patch("fuel.services.geo.requests.get", side_effect=requests.ConnectionError("down"))
    def test_fallback_outage_is_503_not_a_wrong_route(self, *_):
        r = self.client.get("/api/route/", {"start": "San Francisco, CA", "finish": "Miami, FL"})
        self.assertEqual(r.status_code, 503)
