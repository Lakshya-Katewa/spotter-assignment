"""Google encoded-polyline decoding and Douglas-Peucker simplification."""

import numpy as np

# Each value is a delta of at most 360 degrees * 1e5 < 2**26, i.e. at most 6 five-bit
# chunks. Allow 7; anything longer is malformed (and would otherwise grow without
# bound and overflow float conversion).
_MAX_CHUNKS = 7


def decode(encoded: str, precision: int = 5) -> list:
    """Decode an encoded polyline to [[lon, lat], ...] (GeoJSON order).

    Vectorized: one numpy pass over the characters, O(n), no per-character Python loop
    (~1 ms instead of ~30 ms for a cross-country route of 120k characters).
    Raises ValueError for truncated, invalid or oversized input.
    """
    if not isinstance(encoded, str):
        raise ValueError("polyline must be a string")
    if not encoded:
        return []
    try:
        raw = encoded.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid polyline character") from None
    b = np.frombuffer(raw, dtype=np.uint8).astype(np.int64) - 63
    if ((b < 0) | (b >= 64)).any():
        raise ValueError("invalid polyline character")

    # A value is a run of 5-bit chunks; a chunk below 0x20 ends it.
    ends = np.flatnonzero(b < 0x20)
    starts = np.concatenate(([0], ends[:-1] + 1))
    lengths = ends - starts + 1
    tail = len(b) - (ends[-1] + 1 if len(ends) else 0)  # chunks after the last complete value
    if (len(lengths) and lengths.max() > _MAX_CHUNKS) or tail > _MAX_CHUNKS:
        raise ValueError("polyline value too large")
    if tail or len(ends) % 2:  # unfinished value, or a lat without its lon
        raise ValueError("truncated polyline")

    position = np.arange(len(b)) - np.repeat(starts, lengths)  # chunk number within its value
    values = np.add.reduceat((b & 0x1F) << (5 * position), starts)  # chunks don't overlap: sum == or
    deltas = np.where(values & 1, ~(values >> 1), values >> 1)
    factor = 10**precision
    lat = np.cumsum(deltas[0::2]) / factor
    lon = np.cumsum(deltas[1::2]) / factor
    return np.column_stack((lon, lat)).tolist()


def simplify(coords, tolerance_deg: float = 0.0003) -> list:
    """Douglas-Peucker in lon/lat degrees (0.0003 deg ~ 30 m): drops points that don't change the drawn line.

    Distances are to the *segment* between the kept endpoints, not the infinite line
    through them, so a section that doubles back (a U-turn or dead-end spur) is kept.

    Same splits as the classic recursive version, but each level of the recursion is
    one batch: all pending segments are measured together in numpy. That is O(n) work
    per level in a few array operations, instead of one small numpy call per segment
    (~35k calls for a cross-country route).
    """
    pts = np.asarray(coords, dtype=float)
    n = len(pts)
    if n < 3:
        return [list(p) for p in pts]
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    s, e = np.array([0]), np.array([n - 1])
    while len(s):
        inner = e - s - 1
        active = inner > 0
        s, e, inner = s[active], e[active], inner[active]
        if not len(s):
            break
        seg_of = np.repeat(np.arange(len(s)), inner)  # segment of each interior point
        offsets = np.concatenate(([0], np.cumsum(inner)[:-1]))
        idx = s[seg_of] + 1 + (np.arange(len(seg_of)) - offsets[seg_of])

        seg = (pts[e] - pts[s])[seg_of]
        rel = pts[idx] - pts[s][seg_of]
        seg_len2 = seg[:, 0] * seg[:, 0] + seg[:, 1] * seg[:, 1]
        along = rel[:, 0] * seg[:, 0] + rel[:, 1] * seg[:, 1]
        t = np.where(seg_len2 == 0, 0.0, np.clip(along / np.where(seg_len2 == 0, 1.0, seg_len2), 0.0, 1.0))
        d = np.hypot(rel[:, 0] - t * seg[:, 0], rel[:, 1] - t * seg[:, 1])

        worst = np.maximum.reduceat(d, offsets)
        # First interior point reaching the maximum, as argmax would pick.
        first = np.minimum.reduceat(np.where(d == worst[seg_of], np.arange(len(d)), len(d)), offsets)
        split = worst > tolerance_deg
        mid = idx[first[split]]
        keep[mid] = True
        s, e = np.concatenate((s[split], mid)), np.concatenate((mid, e[split]))
    return pts[keep].tolist()
