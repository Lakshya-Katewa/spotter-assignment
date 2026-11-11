"""Place stations at their interstate exit instead of their city centre.

Addresses in the price file often name the exit: "I-44, EXIT 283 & US-69". OSM maps
interstate exits as `highway=motorway_junction` nodes whose `ref` is the exit
number; the interstate comes from the `ref` of the motorway ways the node lies on
("I 80;US 6"). We match a station to the junction nodes with its exit number, on an
interstate its address names, near its city centroid:

  * the junction must be on an interstate named in the address ("I-80/94" names
    both 80 and 94); the same exit number on another interstate never matches;
  * exact exit token first ("27A"), then the bare number ("27");
  * only junctions within MATCH_RADIUS_MILES of the city centroid;
  * if the matches are spread over more than CLUSTER_MILES (the same exit number
    on two different interstates nearby), it's ambiguous and the station keeps
    its city centroid. Both carriageways of one exit are a tight cluster, so the
    result is their mean.

Matching runs offline (`manage.py build_exit_locations`); the result is a small
committed file that `load_stations` applies.
"""

import math
import re
from collections import defaultdict

MATCH_RADIUS_MILES = 15.0
CLUSTER_MILES = 2.0

_INTERSTATE = re.compile(r"\b(?:I|IH|INTERSTATE)\s*-?\s*(\d{1,3})[A-Z]?((?:\s*/\s*\d{1,3}[A-Z]?\b)*)")
_WAY_INTERSTATE = re.compile(r"(?:^|;)\s*I[\s-]*(\d{1,3})")
_EXIT = re.compile(r"\bEXIT\s*#?\s*(\d{1,3})\s*-?\s*([A-Z])?\b")
_REF_PART = re.compile(r"(\d{1,3})([A-Z])?(?:-([A-Z]))?")


def parse_address(address: str):
    """(interstate numbers, exit token) for addresses like "I-80/94, EXIT 27A", else None.

    Interstate numbers are kept in address order, without letter suffixes (I-35E -> "35").
    """
    a = address.upper()
    exit_ = _EXIT.search(a)
    interstates = []
    for m in _INTERSTATE.finditer(a):
        interstates.append(m.group(1))
        interstates += re.findall(r"\d{1,3}", m.group(2))
    if not (interstates and exit_):
        return None
    return tuple(dict.fromkeys(interstates)), exit_.group(1) + (exit_.group(2) or "")


def way_interstates(ref: str) -> frozenset:
    """Interstate numbers in an OSM way ref: "I 80;US 6" -> {"80"}, "I 35E" -> {"35"}."""
    return frozenset(_WAY_INTERSTATE.findall(ref.upper()))


def ref_tokens(ref: str) -> set:
    """OSM junction refs to exit tokens: "27A-B" -> {"27A", "27B"}, "24A;24B" -> {"24A", "24B"}."""
    out = set()
    for part in re.split(r"[;,/]", ref.upper()):
        m = _REF_PART.fullmatch(part.replace(" ", ""))
        if m:
            number, a, b = m.groups()
            out.add(number + (a or ""))
            if b:
                out.add(number + b)
    return out


def build_index(junctions):
    """junctions: iterable of (lat, lon, exit ref, interstate numbers).

    Returns {(interstate, exit token): [(lat, lon), ...]}.
    """
    index = defaultdict(list)
    for lat, lon, ref, interstates in junctions:
        for token in ref_tokens(ref):
            for interstate in interstates:
                index[(interstate, token)].append((lat, lon))
    return index


def _miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3958.7613 * math.asin(math.sqrt(a))


def locate(address, city_lat, city_lon, index):
    """((lat, lon, label) | None, reason). reason is "matched" or why not."""
    parsed = parse_address(address)
    if not parsed:
        return None, "no interstate exit in address"
    interstates, token = parsed
    base = re.match(r"\d+", token).group()
    for key in dict.fromkeys((token, base)):  # exact token, then the bare number
        near, on = [], []
        for interstate in interstates:
            hits = [p for p in index.get((interstate, key), ()) if _miles(city_lat, city_lon, *p) <= MATCH_RADIUS_MILES]
            if hits:
                near += hits
                on.append(interstate)
        if near:
            break
    else:
        return None, "no matching exit near the city"
    near = list(dict.fromkeys(near))  # a junction on two concurrent interstates counts once
    interstate = on[0]
    if max(_miles(*p, *q) for p in near for q in near) > CLUSTER_MILES:
        return None, "ambiguous exit number"
    lat = sum(p[0] for p in near) / len(near)
    lon = sum(p[1] for p in near) / len(near)
    return (round(lat, 6), round(lon, 6), f"I-{interstate} exit {token}"), "matched"
