"""In-memory spatial index of fuel stations and the route-corridor search.

All stations (~6.6k) are loaded once per process into numpy arrays and a
KD-tree over 3D unit vectors, so geographic radius queries are exact and a
corridor search over a cross-country route takes a few milliseconds. The index
is rebuilt when `load_stations` publishes a new DatasetVersion.
"""

import threading
from dataclasses import dataclass

import numpy as np
from django.db import transaction
from scipy.spatial import cKDTree

from fuel.models import DatasetVersion, FuelStation
from fuel.services import highways
from fuel.services import geo
from fuel.services.geo import EARTH_RADIUS_MI

_lock = threading.Lock()
_index = None


def _to_xyz(lat_deg, lon_deg):
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    return np.column_stack((np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)))


def _chord(miles):
    """Straight-line distance on the unit sphere for an arc of `miles`."""
    return 2 * np.sin(np.asarray(miles) / EARTH_RADIUS_MI / 2)


def _arc_miles(chord):
    return 2 * np.arcsin(np.clip(np.asarray(chord) / 2, 0, 1)) * EARTH_RADIUS_MI


@dataclass
class Candidate:
    """A station near the route, positioned by its distance along the route."""

    station_id: int
    mile: float  # distance from the start along the route
    offset_miles: float  # distance from the route line
    price: float
    highways: frozenset  # I-/US- highways named in the station's address


FIELDS = ("opis_id", "lat", "lon", "price", "address", "name", "city", "state", "location_precision", "exit_label")


@dataclass(frozen=True)
class StationInfo:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    lat: float
    lon: float
    price: float
    location_precision: str  # "exit" (OSM interstate exit) or "city" (city centroid)
    exit_label: str


class StationIndex:
    """An immutable snapshot of one dataset version: geometry, prices and the
    details needed for responses all come from the same rows."""

    def __init__(self, rows, version="none"):
        self.version = version
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.lat = np.array([r[1] for r in rows], dtype=float)
        self.lon = np.array([r[2] for r in rows], dtype=float)
        self.price = np.array([float(r[3]) for r in rows], dtype=float)
        self.highways = [highways.from_address(r[4]) for r in rows]
        self.info = {
            r[0]: StationInfo(opis_id=r[0], name=r[5], address=r[4], city=r[6], state=r[7],
                              lat=float(r[1]), lon=float(r[2]), price=float(r[3]),
                              location_precision=r[8], exit_label=r[9])
            for r in rows
        }
        self.tree = cKDTree(_to_xyz(self.lat, self.lon)) if rows else None

    def __len__(self):
        return len(self.ids)

    def along_route(self, coordinates, corridor_miles):
        """Stations within `corridor_miles` of the route, sorted by route mile.

        `coordinates` is the GeoJSON line ([lon, lat] pairs).
        Returns (candidates, cumulative_miles_of_route_points).
        """
        pts, cum = densify(coordinates, step_miles=1.0)
        if self.tree is None or len(pts) == 0:
            return [], cum

        # One nearest-route-point query per station, cut off at the corridor width.
        # A station is in the corridor exactly when its nearest route point is, so this
        # equals a radius search from every route point, without its ~750k intermediate
        # hits on a cross-country route: O(m log n) for m stations and n route points.
        route_xyz = _to_xyz(pts[:, 1], pts[:, 0])
        radius = float(_chord(corridor_miles))
        dist, nearest = cKDTree(route_xyz).query(self.tree.data, distance_upper_bound=np.nextafter(radius, np.inf))
        idx = np.flatnonzero(dist <= radius)
        if idx.size == 0:
            return [], cum
        dist, nearest = dist[idx], nearest[idx]

        # Each nearby station is positioned at its closest route point.
        candidates = [
            Candidate(int(self.ids[i]), float(cum[n]), float(_arc_miles(d)), float(self.price[i]), self.highways[i])
            for i, n, d in zip(idx, nearest, dist)
        ]
        candidates.sort(key=lambda c: (c.mile, c.price))
        return candidates, cum


def densify(coordinates, step_miles=1.0):
    """Interpolate the route so consecutive points are at most `step_miles` apart.

    OSRM geometries can have multi-mile gaps on straight highways; without this
    a station next to such a stretch could fall outside the search radius.
    Returns (points [[lon, lat]], cumulative miles at each point).
    """
    line = np.asarray(coordinates, dtype=float)
    if len(line) < 2:
        return line, np.zeros(len(line))

    lat = np.radians(line[:, 1])
    dlat = np.diff(lat)
    dlon = np.radians(np.diff(line[:, 0]))
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    seg = 2 * EARTH_RADIUS_MI * np.arcsin(np.sqrt(a))

    pieces = np.maximum(1, np.ceil(seg / step_miles).astype(int))
    # For each segment, fractions 0, 1/k, ..., (k-1)/k; then append the final point.
    # Vectorized: position of each output point within its segment, divided by k.
    seg_idx = np.repeat(np.arange(len(seg)), pieces)
    first_of_seg = np.cumsum(pieces) - pieces
    frac = (np.arange(len(seg_idx)) - first_of_seg[seg_idx]) / pieces[seg_idx]
    pts = line[seg_idx] + (line[seg_idx + 1] - line[seg_idx]) * frac[:, None]
    pts = np.vstack([pts, line[-1]])

    seg_start = np.concatenate([[0.0], np.cumsum(seg)])
    cum = seg_start[seg_idx] + seg[seg_idx] * frac
    cum = np.append(cum, seg_start[-1])
    return pts, cum


class DatasetChanging(RuntimeError):
    """The dataset kept changing while we tried to read a consistent snapshot."""


def _read_snapshot(attempts=5):
    """Rows and the version they belong to.

    `load_stations` replaces rows and version in one transaction. We read the
    version before and after the rows (inside a transaction too) and retry if it
    moved, so a snapshot never mixes two loads, whatever the isolation level.
    """
    for _ in range(attempts):
        with transaction.atomic():
            before = DatasetVersion.current()
            rows = list(FuelStation.objects.values_list(*FIELDS))
            after = DatasetVersion.current()
        if before == after:
            return before, rows
    raise DatasetChanging("Station data is being reloaded; please retry.")


def get_index() -> StationIndex:
    """The snapshot for the current dataset version (rebuilt if the version changed)."""
    global _index
    current = DatasetVersion.current()
    if _index is None or _index.version != current:
        with _lock:
            if _index is None or _index.version != current:
                version, rows = _read_snapshot()
                _index = StationIndex(rows, version)
                geo.clear_local_cache()  # places may have been reloaded with the stations
    return _index


def reset_index():
    global _index
    _index = None
    geo.clear_local_cache()
