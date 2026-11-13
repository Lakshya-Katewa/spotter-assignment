"""Pure helpers: names, polylines, highways, the US boundary."""


from django.test import SimpleTestCase

from fuel.services import highways, routing
from fuel.services.boundary import contains
from fuel.services.names import name_variants, normalize, place_key
from fuel.services.polyline import decode, simplify
from fuel.services.stations import densify
from fuel.tests.helpers import (
    encode,
)


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
        for bad in ("_p~iF~ps|U_ul", "_p~iF", "\x00", "_p~iF~ps|Ué", "~~~~~~~~~"):
            with self.assertRaises(ValueError):
                decode(bad)

    def test_simplify_drops_collinear_points(self):
        self.assertEqual(simplify([[0, 0], [1, 0], [2, 0], [3, 0]]), [[0, 0], [3, 0]])

    def test_densify_spacing_and_length(self):
        pts, cum = densify([[-100.0, 40.0], [-100.0, 41.0]], step_miles=1.0)
        self.assertAlmostEqual(cum[-1], 69.09, places=1)
        self.assertLessEqual(max(cum[1:] - cum[:-1]), 1.0 + 1e-9)



class PolylineRobustnessTests(SimpleTestCase):
    def test_oversized_value_is_rejected(self):
        with self.assertRaises(ValueError):
            decode("~" * 220 + "??")
        with self.assertRaises(routing.RoutingError) as ctx:
            routing.parse_osrm(200, {"code": "Ok", "routes": [{"distance": 1, "duration": 1, "geometry": "~" * 220 + "??"}]})
        self.assertEqual(ctx.exception.status, 502)

    def test_extreme_but_valid_values_still_decode(self):
        line = [[-180.0, -90.0], [180.0, 90.0], [-180.0, -90.0]]
        self.assertEqual(decode(encode(line)), line)

    def test_simplify_keeps_backtracking_that_changes_the_drawn_line(self):
        self.assertEqual(simplify([[0, 0], [-1, 0], [1, 0]]), [[0, 0], [-1, 0], [1, 0]])  # excursion behind the start
        self.assertEqual(simplify([[0, 0], [4, 0], [3, 0]]), [[0, 0], [4, 0], [3, 0]])  # overshoot, then come back

    def test_simplify_may_drop_backtracking_that_draws_identically(self):
        # 0 -> 2 -> 1 -> 3 covers exactly the pixels of 0 -> 3; dropping it changes nothing on the map.
        self.assertEqual(simplify([[0, 0], [2, 0], [1, 0], [3, 0]]), [[0, 0], [3, 0]])

    def test_simplify_still_drops_redundant_points(self):
        self.assertEqual(simplify([[0, 0], [1, 0.00001], [2, 0], [3, 0]]), [[0, 0], [3, 0]])



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
