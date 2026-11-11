"""load_stations: reading the price file."""

import os
import tempfile

from django.test import SimpleTestCase

from fuel.management.commands.load_stations import Command


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
