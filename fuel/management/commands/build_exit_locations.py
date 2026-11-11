"""Build data/exit_locations.json: station positions at their interstate exit.

One-time offline job (about 6 minutes; one Overpass query for every interstate
exit in the contiguous US). The output is committed, so normal setup doesn't
need it. Run after `load_stations` (it uses the stations' city centroids), then
run `load_stations` again to apply it.

    python manage.py build_exit_locations [--input junctions.json]
"""

import json
from collections import Counter
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services import exits

OUTPUT = Path(settings.BASE_DIR) / "data" / "exit_locations.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
QUERY = (
    "[out:json][timeout:900];"
    'way[highway=motorway][ref~"(^|;) ?I ?[0-9]"](24.0,-125.0,49.5,-66.5);'
    "node(w)[highway=motorway_junction][ref];out;"
)


class Command(BaseCommand):
    help = "Match stations to OpenStreetMap interstate exits and write data/exit_locations.json."

    def add_arguments(self, parser):
        parser.add_argument("--input", help="Use a saved Overpass JSON response instead of downloading.")

    def handle(self, *args, **opts):
        if opts["input"]:
            data = json.loads(Path(opts["input"]).read_text())
        else:
            self.stdout.write("Querying Overpass for all interstate exits (takes several minutes)...")
            r = requests.post(OVERPASS_URL, data={"data": QUERY}, timeout=1000,
                              headers={"User-Agent": settings.HTTP_USER_AGENT})
            r.raise_for_status()
            data = r.json()
        junctions = [(e["lat"], e["lon"], e["tags"]["ref"]) for e in data.get("elements", [])
                     if e.get("type") == "node" and e.get("tags", {}).get("ref")]
        if not junctions:
            raise CommandError("No junctions in the Overpass response.")
        index = exits.build_index(junctions)

        stations = FuelStation.objects.values_list("opis_id", "address", "city_lat", "city_lon")
        if not stations:
            raise CommandError("No stations loaded: run load_stations first.")
        out, reasons = {}, Counter()
        for opis_id, address, city_lat, city_lon in stations:
            found, reason = exits.locate(address, city_lat, city_lon, index)
            reasons[reason] += 1
            if found:
                out[str(opis_id)] = list(found)

        OUTPUT.write_text(json.dumps(
            {"source": "OpenStreetMap contributors (ODbL), via Overpass", "junctions": len(junctions), "stations": out},
            indent=0, sort_keys=True))
        total = sum(reasons.values())
        summary = ", ".join(f"{k}: {v} ({100 * v / total:.0f}%)" for k, v in reasons.most_common())
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(out)} exit locations to {OUTPUT}. {summary}"))
