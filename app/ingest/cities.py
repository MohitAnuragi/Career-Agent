"""Canonical Indian cities and their aliases: one table for ingest, search, preferences and the UI.

A job's `cities` holds the specific cities it names ("Gurugram") plus the region they belong to
("Delhi NCR"), so a filter on either finds it.
"""
from __future__ import annotations

import re

CITIES: dict[str, tuple[str, ...]] = {
    "Bengaluru": ("bengaluru", "bangalore", "blr"),
    "Hyderabad": ("hyderabad", "secunderabad", "hitec city", "hitech city", "gachibowli"),
    "Pune": ("pune", "poona", "hinjewadi"),
    "Delhi": ("delhi", "new delhi"),
    "Gurugram": ("gurugram", "gurgaon"),
    "Noida": ("noida", "greater noida"),
    "Ghaziabad": ("ghaziabad",),
    "Faridabad": ("faridabad",),
    "Mumbai": ("mumbai", "bombay", "navi mumbai", "thane", "powai"),
    "Chennai": ("chennai", "madras"),
    "Kolkata": ("kolkata", "calcutta"),
    "Ahmedabad": ("ahmedabad", "gandhinagar"),
    "Jaipur": ("jaipur",),
    "Kochi": ("kochi", "cochin"),
    "Chandigarh": ("chandigarh", "mohali"),
    "Indore": ("indore",),
    "Coimbatore": ("coimbatore",),
    "Thiruvananthapuram": ("thiruvananthapuram", "trivandrum"),
    "Mysuru": ("mysuru", "mysore"),
}

# Regions: a posting in any member city is also tagged with the region.
REGIONS: dict[str, tuple[str, ...]] = {
    "Delhi NCR": ("Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad"),
}
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "Delhi NCR": ("delhi ncr", "ncr"),
}

# Order of the city chips in the UI.
FEATURED = ["Bengaluru", "Hyderabad", "Pune", "Delhi NCR", "Mumbai", "Chennai"]

_ALIAS_TO_CITY = {alias: city for city, aliases in CITIES.items() for alias in aliases}
_ALIAS_TO_REGION = {alias: region for region, aliases in REGION_ALIASES.items() for alias in aliases}
_CANON = {name.lower(): name for name in [*CITIES, *REGIONS]}

# Longest alias first, so "new delhi" wins over "delhi" and "navi mumbai" over "mumbai".
ALL_ALIASES = sorted({*_ALIAS_TO_CITY, *_ALIAS_TO_REGION}, key=len, reverse=True)
_CITY_RE = re.compile(
    r"\b(" + "|".join(r"[\s-]+".join(map(re.escape, a.split())) for a in ALL_ALIASES) + r")\b",
    re.IGNORECASE,
)


def _norm(alias: str) -> str:
    return re.sub(r"[\s-]+", " ", alias.lower())


def extract_cities(*texts: str | None) -> list[str]:
    """Canonical cities named in free-text locations, in order of appearance, plus their regions."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for m in _CITY_RE.finditer(text):
            alias = _norm(m.group(1))
            name = _ALIAS_TO_CITY.get(alias) or _ALIAS_TO_REGION.get(alias)
            if name and name not in found:
                found.append(name)
    for region, members in REGIONS.items():
        if region not in found and any(c in found for c in members):
            found.append(region)
    return found


def canonical_cities(value: str | None) -> list[str]:
    """Map what a user typed ("gurgaon", "ncr", "Bangalore, India") to canonical names. Unknown -> []."""
    if not value or not value.strip():
        return []
    key = _norm(value.strip())
    if key in _CANON:
        return [_CANON[key]]
    if key in _ALIAS_TO_REGION:
        return [_ALIAS_TO_REGION[key]]
    if key in _ALIAS_TO_CITY:
        return [_ALIAS_TO_CITY[key]]
    # Free text: keep the specific cities, drop regions that were only implied by a member city.
    named = extract_cities(value)
    implied = {r for r, members in REGIONS.items() if any(c in named for c in members)}
    explicit_regions = {_ALIAS_TO_REGION[_norm(m.group(1))] for m in _CITY_RE.finditer(value)
                        if _norm(m.group(1)) in _ALIAS_TO_REGION}
    return [c for c in named if c not in implied or c in explicit_regions]


def strip_cities(text: str) -> str:
    """Remove city names from text, e.g. 'Backend Engineer - Bangalore' -> 'Backend Engineer - '."""
    return _CITY_RE.sub(" ", text)


def canonical_list(values: list[str] | None) -> list[str]:
    """Canonicalise a list of user-entered locations; entries that aren't known cities are kept as typed."""
    out: list[str] = []
    for v in values or []:
        names = canonical_cities(v) or [v.strip()]
        for n in names:
            if n and n.lower() not in {o.lower() for o in out}:
                out.append(n)
    return out
