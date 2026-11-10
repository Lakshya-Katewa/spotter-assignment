"""Load US places (Census Gazetteer) and fuel stations (price CSV) into the DB.

The price CSV has no coordinates, so each station is placed at the centroid of
its city. Cities are matched against the Census Gazetteer place and county
subdivision files. The few names that still don't match are geocoded once with
Nominatim (`--geocode-missing`) and cached in data/geocode_overrides.json, so
normal loads never touch the network.
"""

import csv
import io
import json
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel.models import FuelStation, Place
from fuel.services.names import CANADIAN_PROVINCES, name_variants, place_key

DATA = Path(settings.BASE_DIR) / "data"
PLACES_FILE = DATA / "2024_Gaz_place_national.txt"
COUSUBS_FILE = DATA / "2024_Gaz_cousubs_national.txt"
OVERRIDES_FILE = DATA / "geocode_overrides.json"
GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/{name}.zip"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def _read_gazetteer(path: Path):
    with path.open(encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        header = [h.strip() for h in next(reader)]
        for row in reader:
            yield dict(zip(header, (c.strip() for c in row)))


class Command(BaseCommand):
    help = "Load Census places and fuel stations (with coordinates) into the database."

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=str(Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"))
        parser.add_argument(
            "--geocode-missing",
            action="store_true",
            help="Geocode unmatched cities with Nominatim (1 req/s) and cache them in data/geocode_overrides.json.",
        )

    def handle(self, *args, **opts):
        for path in (PLACES_FILE, COUSUBS_FILE):
            if not path.exists():
                self._download_gazetteer(path)
        places = self._build_places()
        overrides = json.loads(OVERRIDES_FILE.read_text()) if OVERRIDES_FILE.exists() else {}

        stations = self._read_stations(opts["csv"])
        missing = sorted({place_key(s["city"], s["state"]) for s in stations.values()} - places.keys() - overrides.keys())

        if missing and opts["geocode_missing"]:
            overrides.update(self._geocode(missing))
            OVERRIDES_FILE.write_text(json.dumps(overrides, indent=1, sort_keys=True))
            missing = [k for k in missing if k not in overrides]

        # Census matches win; Nominatim results only fill gaps.
        lookup = {k: tuple(v) for k, v in overrides.items() if v}
        lookup.update({k: (v[0], v[1]) for k, v in places.items()})

        objs, skipped = [], 0
        for s in stations.values():
            coords = lookup.get(place_key(s["city"], s["state"]))
            if not coords:
                skipped += 1
                continue
            objs.append(FuelStation(**s, lat=coords[0], lon=coords[1]))

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(objs, batch_size=2000)

        self.stdout.write(self.style.SUCCESS(f"Loaded {len(objs)} stations ({skipped} skipped: city not geocoded)."))
        if missing:
            self.stdout.write(f"Unmatched cities ({len(missing)}): {', '.join(missing[:30])}")

    def _download_gazetteer(self, path: Path):
        url = GAZETTEER_URL.format(name=path.stem)
        self.stdout.write(f"Downloading {url} ...")
        r = requests.get(url, timeout=120, headers={"User-Agent": settings.HTTP_USER_AGENT})
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(path.name))
            DATA.mkdir(exist_ok=True)
            path.write_bytes(zf.read(member))

    # -- places -----------------------------------------------------------
    def _build_places(self):
        """Return {key: (lat, lon, rank, name, state)} and store all places in the DB."""
        best = {}

        def add(row, source_rank):
            lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            for depth, variant in enumerate(name_variants(row["NAME"])):
                rank = source_rank + (5 if depth >= 2 else 0)
                key = f"{variant}|{row['USPS']}"
                if key not in best or rank < best[key][2]:
                    best[key] = (lat, lon, rank, row["NAME"], row["USPS"])

        for row in _read_gazetteer(PLACES_FILE):
            add(row, 0 if row.get("FUNCSTAT") == "A" else 1)
        for row in _read_gazetteer(COUSUBS_FILE):
            add(row, 2)

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(
                [Place(key=k, name=v[3], state=v[4], lat=v[0], lon=v[1], rank=v[2]) for k, v in best.items()],
                batch_size=5000,
            )
        self.stdout.write(f"Loaded {len(best)} place names.")
        return best

    # -- stations ---------------------------------------------------------
    def _read_stations(self, path):
        """Dedupe by OPIS id, keeping the lowest price; drop Canadian rows."""
        stations = {}
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                state = row["State"].strip().upper()
                if state in CANADIAN_PROVINCES:
                    continue
                opis_id = int(row["OPIS Truckstop ID"])
                price = Decimal(row["Retail Price"]).quantize(Decimal("0.0001"))
                if opis_id in stations and stations[opis_id]["price"] <= price:
                    continue
                stations[opis_id] = {
                    "opis_id": opis_id,
                    "name": row["Truckstop Name"].strip(),
                    "address": row["Address"].strip(),
                    "city": row["City"].strip(),
                    "state": state,
                    "price": price,
                }
        return stations

    def _geocode(self, keys):
        out = {}
        session = requests.Session()
        session.headers["User-Agent"] = "fuel-route-planner/1.0 (assessment project)"
        for i, key in enumerate(keys):
            city, state = key.split("|")
            try:
                r = session.get(
                    NOMINATIM_URL,
                    params={"city": city, "state": state, "country": "USA", "format": "json", "limit": 1},
                    timeout=15,
                )
                r.raise_for_status()
                hits = r.json()
                out[key] = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
            except requests.RequestException as exc:
                self.stderr.write(f"Geocoding {key} failed: {exc}")
            self.stdout.write(f"[{i + 1}/{len(keys)}] {key} -> {out.get(key)}")
            time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
        return out
