"""Build data/us_boundary.json from the Census cartographic boundary file.

Source: cb_2024_us_nation_5m (US Census Bureau, public domain). The output is
committed, so this only needs re-running to refresh the boundary. Reads the
shapefile directly (polygon records only), so no GIS dependency is needed.
"""

import io
import json
import struct
import zipfile

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from fuel.services.boundary import BOUNDARY_FILE

URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_nation_5m.zip"


def read_polygon_rings(shp: bytes):
    """Yield rings ([[lon, lat], ...]) from every Polygon/MultiPolygon record of a .shp file."""
    pos = 100  # fixed-size file header
    while pos + 8 <= len(shp):
        _, length_words = struct.unpack(">ii", shp[pos : pos + 8])
        content = shp[pos + 8 : pos + 8 + length_words * 2]
        pos += 8 + length_words * 2
        (shape_type,) = struct.unpack("<i", content[:4])
        if shape_type not in (5, 15, 25):  # Polygon, PolygonZ, PolygonM
            continue
        num_parts, num_points = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", content[44 : 44 + 4 * num_parts]))
        pts_off = 44 + 4 * num_parts
        coords = struct.unpack(f"<{2 * num_points}d", content[pts_off : pts_off + 16 * num_points])
        points = [[round(coords[2 * i], 5), round(coords[2 * i + 1], 5)] for i in range(num_points)]
        for start, end in zip(parts, parts[1:] + [num_points]):
            yield points[start:end]


class Command(BaseCommand):
    help = "Download the Census US boundary and store it as data/us_boundary.json."

    def handle(self, *args, **opts):
        r = requests.get(URL, timeout=120, headers={"User-Agent": settings.HTTP_USER_AGENT})
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            shp = zf.read(next(n for n in zf.namelist() if n.endswith(".shp")))
        rings = list(read_polygon_rings(shp))
        BOUNDARY_FILE.write_text(json.dumps({"source": URL, "rings": rings}, separators=(",", ":")))
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(rings)} rings, {sum(map(len, rings))} points to {BOUNDARY_FILE}")
        )
