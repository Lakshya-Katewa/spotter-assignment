"""Highway references ("I44", "US69") from station addresses and OSRM route steps.

Price-file addresses look like "I-44, EXIT 283 & US-69": the station sits at an
exit of that highway. If the route drives that highway near the station's
position, the station is reachable from the route without a real detour, even
though we only know its city's centroid. Only Interstate and US routes are
used; state-route naming differs too much between sources.
"""

import re

_INTERSTATE = re.compile(r"\b(?:I|IH|INTERSTATE)\s*-?\s*(\d{1,3})\b")
_US_ROUTE = re.compile(r"\bUS\s*-?\s*(?:HWY|HIGHWAY|ROUTE|RT)?\s*-?\s*(\d{1,3})\b")


def from_address(address: str) -> frozenset:
    a = address.upper()
    return frozenset({f"I{n}" for n in _INTERSTATE.findall(a)} | {f"US{n}" for n in _US_ROUTE.findall(a)})


def from_osrm_ref(ref: str) -> frozenset:
    """OSRM step refs look like "I 30; US 70"."""
    out = set()
    for token in (ref or "").split(";"):
        t = token.strip().upper().replace(" ", "").replace("-", "")
        m = re.fullmatch(r"(I|US)(\d{1,3})[A-Z]?", t)
        if m:
            out.add(f"{m.group(1)}{m.group(2)}")
    return frozenset(out)


def spans_from_steps(steps, meters_per_mile):
    """[(start_mile, end_mile, refs)] for every route step that uses an I-/US- highway."""
    spans, pos = [], 0.0
    for step in steps:
        length = step.get("distance", 0.0) / meters_per_mile
        refs = from_osrm_ref(step.get("ref", ""))
        if refs and length > 0:
            spans.append((pos, pos + length, refs))
        pos += length
    return spans


def refs_near(spans, mile, window):
    """Highways the route uses within `window` miles of `mile`."""
    out = set()
    for start, end, refs in spans:
        if start <= mile + window and end >= mile - window:
            out |= refs
    return out
