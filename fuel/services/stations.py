"""In-memory spatial index of fuel stations and the route-corridor search.

All stations (~6.6k) are loaded once per process into numpy arrays and a
KD-tree over 3D unit vectors, so geographic radius queries are exact and a
corridor search over a cross-country route takes a few milliseconds.
"""

import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from fuel.models import FuelStation
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


class StationIndex:
    def __init__(self, rows):
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.lat = np.array([r[1] for r in rows], dtype=float)
        self.lon = np.array([r[2] for r in rows], dtype=float)
        self.price = np.array([float(r[3]) for r in rows], dtype=float)
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

        route_xyz = _to_xyz(pts[:, 1], pts[:, 0])
        hits = self.tree.query_ball_point(route_xyz, r=float(_chord(corridor_miles)))
        idx = np.unique(np.fromiter((i for h in hits for i in h), dtype=np.int64))
        if idx.size == 0:
            return [], cum

        # Position each nearby station at its closest route point.
        dist, nearest = cKDTree(route_xyz).query(self.tree.data[idx])
        candidates = [
            Candidate(int(self.ids[i]), float(cum[n]), float(_arc_miles(d)), float(self.price[i]))
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
    seg_idx = np.repeat(np.arange(len(seg)), pieces)
    frac = np.concatenate([np.arange(k) / k for k in pieces])
    pts = line[seg_idx] + (line[seg_idx + 1] - line[seg_idx]) * frac[:, None]
    pts = np.vstack([pts, line[-1]])

    seg_start = np.concatenate([[0.0], np.cumsum(seg)])
    cum = seg_start[seg_idx] + seg[seg_idx] * frac
    cum = np.append(cum, seg_start[-1])
    return pts, cum


def get_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = StationIndex(list(FuelStation.objects.values_list("opis_id", "lat", "lon", "price")))
    return _index


def reset_index():
    global _index
    _index = None
