"""Google encoded-polyline decoding and Douglas-Peucker simplification."""

import numpy as np


def decode(encoded: str, precision: int = 5) -> list:
    """Decode an encoded polyline to [[lon, lat], ...] (GeoJSON order).

    Raises ValueError for truncated or invalid input.
    """
    if not isinstance(encoded, str):
        raise ValueError("polyline must be a string")
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10**precision
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            result, shift = 0, 0
            while True:
                if index >= length:
                    raise ValueError("truncated polyline")
                b = ord(encoded[index]) - 63
                if not 0 <= b < 64:
                    raise ValueError("invalid polyline character")
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append([lon / factor, lat / factor])
    return coords


def simplify(coords, tolerance_deg: float = 0.0003) -> list:
    """Douglas-Peucker in lon/lat degrees (0.0003 deg ~ 30 m): drops points that don't change the drawn line."""
    pts = np.asarray(coords, dtype=float)
    n = len(pts)
    if n < 3:
        return [list(p) for p in pts]
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        seg = pts[e] - pts[s]
        rel = pts[s + 1 : e] - pts[s]
        norm = np.hypot(*seg)
        if norm == 0:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / norm
        i = int(np.argmax(d))
        if d[i] > tolerance_deg:
            m = s + 1 + i
            keep[m] = True
            stack.append((s, m))
            stack.append((m, e))
    return pts[keep].tolist()
