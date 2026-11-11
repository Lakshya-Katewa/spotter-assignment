"""Placing stations at their interstate exit (OSM) and using that position on routes."""

import io
import json
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from fuel.management.commands import build_exit_locations
from fuel.models import DatasetVersion, FuelStation
from fuel.services import exits
from fuel.services.boundary import contains
from fuel.tests.helpers import ApiFixture, fake_route


class ParsingTests(SimpleTestCase):
    def test_parse_address(self):
        self.assertEqual(exits.parse_address("I-44, EXIT 283 & US-69"), ("44", "283"))
        self.assertEqual(exits.parse_address("I-80, EXIT 27A & US-46"), ("80", "27A"))
        self.assertEqual(exits.parse_address("I-81, EXIT 59-A"), ("81", "59A"))
        self.assertEqual(exits.parse_address("I-80/94, EXIT 2"), ("80", "2"))
        self.assertIsNone(exits.parse_address("US-46/US-1/US-9"))  # no interstate
        self.assertIsNone(exits.parse_address("I-35 & MAIN ST"))  # no exit number

    def test_ref_tokens(self):
        self.assertEqual(exits.ref_tokens("27A-B"), {"27A", "27B"})
        self.assertEqual(exits.ref_tokens("24A;24B"), {"24A", "24B"})
        self.assertEqual(exits.ref_tokens("143"), {"143"})
        self.assertEqual(exits.ref_tokens("10 A"), {"10A"})
        self.assertEqual(exits.ref_tokens("Toll Plaza"), set())


class LocateTests(SimpleTestCase):
    CITY = (40.0, -100.0)

    def index(self, *junctions):
        return exits.build_index(junctions)

    def test_both_carriageways_average_to_the_exit(self):
        idx = self.index((40.050, -100.000, "27A"), (40.050, -100.004, "27A"))
        (lat, lon, label), reason = exits.locate("I-80, EXIT 27A", *self.CITY, idx)
        self.assertEqual(reason, "matched")
        self.assertAlmostEqual(lon, -100.002)
        self.assertEqual(label, "I-80 exit 27A")

    def test_falls_back_to_bare_number(self):
        idx = self.index((40.05, -100.0, "27"))
        found, reason = exits.locate("I-80, EXIT 27A", *self.CITY, idx)
        self.assertEqual(reason, "matched")
        self.assertEqual(found[:2], (40.05, -100.0))

    def test_far_or_ambiguous_exits_keep_the_city_centroid(self):
        far = self.index((41.0, -100.0, "27"))  # ~69 miles away: a different exit 27
        self.assertEqual(exits.locate("I-80, EXIT 27", *self.CITY, far), (None, "no matching exit near the city"))
        two = self.index((40.05, -100.0, "27"), (40.0, -100.1, "27"))  # same number on two interstates nearby
        self.assertEqual(exits.locate("I-80, EXIT 27", *self.CITY, two), (None, "ambiguous exit number"))
        self.assertEqual(exits.locate("MAIN ST", *self.CITY, two), (None, "no interstate exit in address"))


class ExitStationOnRouteTests(ApiFixture):
    """Exit-placed stations are judged by their real distance from the route."""

    def add(self, opis, name, lat, lon, price, label):
        FuelStation.objects.create(
            opis_id=opis, name=name, address=label.replace(" exit ", ", EXIT "), city="X", state="NE",
            price=Decimal(price), lat=lat, lon=lon, city_lat=lat, city_lon=lon - 0.1,
            location_precision="exit", exit_label=label,
        )
        DatasetVersion.objects.filter(pk=1).update(version=f"v-{opis}")

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_exit_on_route_is_used_and_labelled(self, _):
        self.add(20, "EXIT ON ROUTE", 44.5, -100.002, "1.10", "I-99 exit 150")  # ~0.1 mi from the line
        stop = {s["name"]: s for s in self.get().json()["fuel_stops"]}["EXIT ON ROUTE"]
        self.assertEqual(stop["location_precision"], "highway exit")
        self.assertTrue(stop["route_match"].startswith("matched to I-99 exit 150, 0."), stop["route_match"])

    @mock.patch("fuel.services.routing.get_route", side_effect=fake_route)
    def test_exit_off_route_is_rejected_even_if_highway_name_matches(self, _):
        # Address names I-99 (which the route uses), but the exact position is ~4 mi away:
        # position evidence wins over the name match.
        self.add(21, "EXIT OFF ROUTE", 44.5, -100.08, "0.50", "I-99 exit 151")
        names = [s["name"] for s in self.get().json()["fuel_stops"]]
        self.assertNotIn("EXIT OFF ROUTE", names)


class BuildCommandTests(TestCase):
    """build_exit_locations end to end on a small saved Overpass response."""

    def test_command_matches_stations_and_writes_output(self):
        DatasetVersion.objects.create(pk=1, version="v1")
        common = dict(city="X", state="NE", price=Decimal("3.00"), lat=0, lon=0, city_lat=40.0, city_lon=-100.0)
        FuelStation.objects.create(opis_id=1, name="ON EXIT", address="I-80, EXIT 27A & US-46", **common)
        FuelStation.objects.create(opis_id=2, name="NO EXIT", address="US-46 & MAIN ST", **common)
        FuelStation.objects.create(opis_id=3, name="AMBIGUOUS", address="I-80, EXIT 5", **common)
        overpass = {"elements": [
            {"type": "node", "lat": 40.05, "lon": -100.000, "tags": {"ref": "27A-B"}},
            {"type": "node", "lat": 40.05, "lon": -100.004, "tags": {"ref": "27A"}},
            {"type": "node", "lat": 40.02, "lon": -100.0, "tags": {"ref": "5"}},
            {"type": "node", "lat": 40.0, "lon": -100.12, "tags": {"ref": "5"}},  # same number, ~6 mi away
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "junctions.json", Path(tmp) / "exit_locations.json"
            source.write_text(json.dumps(overpass))
            with mock.patch.object(build_exit_locations, "OUTPUT", output):  # never touch the committed file
                call_command("build_exit_locations", input=str(source), stdout=io.StringIO())
            written = json.loads(output.read_text())["stations"]
        self.assertEqual(set(written), {"1"})
        lat, lon, label = written["1"]
        self.assertEqual((lat, round(lon, 3), label), (40.05, -100.002, "I-80 exit 27A"))


class CommittedExitDataTests(SimpleTestCase):
    """Integrity of data/exit_locations.json, which load_stations applies."""

    def test_entries_are_well_formed_and_inside_the_us(self):
        data = json.loads((Path(settings.BASE_DIR) / "data" / "exit_locations.json").read_text())
        stations = data["stations"]
        self.assertGreater(len(stations), 3000)
        label = re.compile(r"I-\d{1,3} exit \d{1,3}[A-Z]?")
        for opis_id, (lat, lon, text) in stations.items():
            self.assertTrue(opis_id.isdigit(), opis_id)
            self.assertRegex(text, label)
            self.assertTrue(contains(lat, lon), (opis_id, lat, lon))
