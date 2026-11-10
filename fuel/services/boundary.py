"""Offline "is this point in the USA?" check against the Census national boundary.

The boundary (data/us_boundary.json, built by `manage.py build_us_boundary`) is
loaded once per process. A point is inside if an odd number of rings contain it
(even-odd rule), so holes such as lakes are handled. Each check is a few
vectorized numpy passes over the rings whose bounding box contains the point.
"""

import json
import threading
from pathlib import Path

import numpy as np
from django.conf import settings

BOUNDARY_FILE = Path(settings.BASE_DIR) / "data" / "us_boundary.json"

_lock = threading.Lock()
_rings = None  # list of (bbox, x array, y array)


def _load():
    global _rings
    if _rings is None:
        with _lock:
            if _rings is None:
                data = json.loads(BOUNDARY_FILE.read_text())
                rings = []
                for ring in data["rings"]:
                    a = np.asarray(ring, dtype=float)
                    bbox = (a[:, 0].min(), a[:, 0].max(), a[:, 1].min(), a[:, 1].max())
                    rings.append((bbox, a[:, 0], a[:, 1]))
                _rings = rings
    return _rings


def _in_ring(x, y, xs, ys):
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    crosses = (ys > y) != (yj > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_at_y = (xj - xs) * (y - ys) / (yj - ys) + xs
    return bool(np.count_nonzero(crosses & (x < x_at_y)) % 2)


def contains(lat: float, lon: float) -> bool:
    inside = False
    for (x0, x1, y0, y1), xs, ys in _load():
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, xs, ys):
            inside = not inside
    return inside
