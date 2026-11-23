"""Read filters out of a plain-language search, e.g. "remote backend jobs in pune" -> "backend" + Pune + remote.

The filter words are removed from the text, so they don't count as keywords: "pune" matches every
posting that names Pune, which drowns out the role the person asked for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ingest.cities import ALL_ALIASES, canonical_cities

_CITY = "|".join(r"[\s-]+".join(map(re.escape, a.split())) for a in ALL_ALIASES)
_LOCATION = re.compile(rf"\b(?:(?:based\s+)?(?:in|at|near|around)\s+)?({_CITY})\b", re.IGNORECASE)
_REMOTE = re.compile(r"\b(?:fully\s+)?(?:remote(?:ly)?|wfh|work[\s-]from[\s-]home)(?:\s+(?:only|first|friendly))?\b",
                     re.IGNORECASE)
_INDIA = re.compile(r"\b(?:(?:in|across)\s+)?india\b", re.IGNORECASE)
# Words that only say "a job" and carry no meaning for ranking once the filters are gone.
_FILLER = re.compile(r"\b(?:jobs?|roles?|openings?|positions?|vacanc(?:y|ies)|opportunit(?:y|ies)|"
                     r"show me|find me|looking for)\b", re.IGNORECASE)
_EDGE = re.compile(r"^(?:\W|\b(?:in|at|for|and|or|only|any|the|a|an)\b)+|(?:\W|\b(?:in|at|for|and|or|only|any|the|a|an)\b)+$",
                   re.IGNORECASE)


@dataclass
class ParsedQuery:
    text: str  # what is left to rank on; "" when the query was only filters
    cities: list[str] = field(default_factory=list)
    remote: bool = False
    india: bool = False

    @property
    def has_filters(self) -> bool:
        return bool(self.cities or self.remote or self.india)


def parse_query(query: str) -> ParsedQuery:
    cities: list[str] = []

    def take_city(m: re.Match) -> str:
        for c in canonical_cities(m.group(1)):
            if c not in cities:
                cities.append(c)
        return " "

    text = _LOCATION.sub(take_city, query)
    text, n_remote = _REMOTE.subn(" ", text)
    text, n_india = _INDIA.subn(" ", text)
    if not (cities or n_remote or n_india):
        return ParsedQuery(text=query.strip())
    text = _FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _EDGE.sub("", text).strip()
    return ParsedQuery(text=text, cities=cities, remote=bool(n_remote), india=bool(n_india))
