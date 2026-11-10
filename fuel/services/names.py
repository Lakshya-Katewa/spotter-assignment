"""Normalization of US place names so CSV cities match Census Gazetteer names."""

import re

# Legal/statistical-area descriptors appended to Gazetteer names
# ("Abbeville city", "Abanda CDP", "Nashville-Davidson metropolitan government (balance)").
_DESCRIPTOR = re.compile(
    r"\s+(city and borough|(?:consolidated|metro|metropolitan|unified) government \(balance\)"
    r"|\(balance\)|city|town|township|village|borough|cdp|ccd|municipality|plantation"
    r"|charter township|gore|grant|location|purchase|unorganized territory|precinct)$",
    re.IGNORECASE,
)

CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def normalize(name: str) -> str:
    s = name.strip().lower()
    s = s.replace(".", "").replace("'", "").replace("-", " ")
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bsainte\b", "ste", s)
    s = re.sub(r"^ft\b", "fort", s)
    s = re.sub(r"^mt\b", "mount", s)
    # Spacing is inconsistent between sources ("Mc Alpin"/"McAlpin", "Brookpark"/"Brook Park").
    return re.sub(r"\s+", "", s)


def name_variants(gazetteer_name: str) -> list[str]:
    """All normalized forms of a Gazetteer name, stripping descriptors one at a time.

    "Bay City city" -> ["baycitycity", "baycity", "bay"]. Matching uses the
    first form found, so "Bay City" in the CSV matches the right place.
    """
    out = []
    n = gazetteer_name.strip()
    while True:
        norm = normalize(n)
        if norm and norm not in out:
            out.append(norm)
        stripped = _DESCRIPTOR.sub("", n)
        if stripped == n:
            break
        n = stripped
    # "Athens-Clarke County unified government (balance)" -> also "athens"
    if "-" in gazetteer_name:
        head = normalize(gazetteer_name.split("-")[0])
        if head and head not in out:
            out.append(head)
    return out


def place_key(name: str, state: str) -> str:
    return f"{normalize(name)}|{state.strip().upper()}"
