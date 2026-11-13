"""load_stations: reading the price file."""

import os
import tempfile

from django.test import SimpleTestCase, TestCase

from fuel.management.commands.load_stations import Command
from fuel.models import FuelStation


class DuplicatePriceTests(SimpleTestCase):
    """Duplicate OPIS ids (no dates in the file) use the median price, not the minimum."""

    def test_median_of_duplicates(self):
        header = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        rows = ["105,TA,\"I-75, EXIT 144-B\",Bridgeport,MI,260,3.269\n",
                "105,TA,\"I-75, EXIT 144-B\",Bridgeport,MI,260,3.429\n",
                "105,TA,\"I-75, EXIT 144-B\",Bridgeport,MI,260,3.299\n",
                "7,ONE,MAIN ST,X,NE,1,3.10\n",
                "8,TWO,MAIN ST,X,NE,1,3.00\n",
                "8,TWO,MAIN ST,X,NE,1,3.20\n",
                "9,CANADA,MAIN ST,X,ON,1,1.00\n"]
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(header + "".join(rows))
        self.addCleanup(os.unlink, f.name)
        stations = Command()._read_stations(f.name)
        self.assertEqual(str(stations[105]["price"]), "3.2990")
        self.assertEqual(str(stations[7]["price"]), "3.1000")
        self.assertEqual(str(stations[8]["price"]), "3.1000")  # even count: mean of the middle two
        self.assertNotIn(9, stations)


class RunserverSetupTests(TestCase):
    """`runserver` prepares a fresh database itself; nothing else changes the database."""

    def test_runserver_is_ours(self):
        from django.core.management import get_commands
        self.assertEqual(get_commands()["runserver"], "fuel")

    def test_loads_stations_only_when_missing(self):
        from io import StringIO
        from unittest import mock

        from fuel.management.commands.runserver import prepare_database
        with mock.patch("fuel.management.commands.runserver.call_command") as call:
            prepare_database(StringIO())  # migrated test database, no stations yet
        call.assert_called_once_with("load_stations")

        FuelStation.objects.create(opis_id=1, name="X", address="I-1", city="X", state="NE", price="3.0", lat=40, lon=-100)
        with mock.patch("fuel.management.commands.runserver.call_command") as call:
            prepare_database(StringIO())
        call.assert_not_called()
