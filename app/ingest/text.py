"""Text cleanup and location classification for raw job-board payloads."""
from __future__ import annotations

import hashlib
import html
import re

from bs4 import BeautifulSoup

from app.ingest.cities import ALL_ALIASES

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "blockquote"]

# Word boundaries matter: "Indiana" and "Indianapolis" are not India. Built from the city table so the two can't drift.
_INDIA = re.compile(
    r"\b(india|" + "|".join(r"[\s-]+".join(map(re.escape, a.split())) for a in ALL_ALIASES) + r")\b",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.IGNORECASE)

# Remote scope: who can actually take a remote role.
_REMOTE_INDIA = re.compile(r"\b(india|ist|apac|asia)\b", re.IGNORECASE)
_REMOTE_GLOBAL = re.compile(r"\b(anywhere|worldwide|global|international)\b", re.IGNORECASE)
_REMOTE_RESTRICTED = re.compile(
    r"\b(us|usa|u\.s\.?|united states|americas?|north america|canada|mexico|latam|brazil|uk|united kingdom|"
    r"europe|eu|emea|germany|france|spain|portugal|netherlands|ireland|poland|israel|australia|japan|singapore|"
    r"new york|san francisco|seattle|austin|london|berlin|toronto)\b",
    re.IGNORECASE,
)
_INDIA_COUNTRY = {"in", "ind", "india"}
# Words in a location that don't name a place.
_REMOTE_FILLER = re.compile(
    r"\b(remote|remotely|work from home|wfh|hybrid|flexible|full[- ]?time|part[- ]?time|first|friendly|only|"
    r"based|location|locations|or|and|office|home)\b",
    re.IGNORECASE,
)


def html_to_text(raw: str | None, *, escaped: bool = False) -> str:
    """Convert (optionally HTML-entity-escaped) HTML to readable plain text."""
    if not raw:
        return ""
    if escaped:  # Greenhouse returns HTML with entities escaped: &lt;div&gt;
        raw = html.unescape(raw)
    soup = BeautifulSoup(raw, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("- ")
    for block in soup.find_all(_BLOCK_TAGS):  # block tags end a line; inline tags (<b>, <a>) don't
        block.insert_after("\n")
    text = soup.get_text()
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def is_india(*texts: str | None) -> bool:
    return any(t and _INDIA.search(t) for t in texts)


def is_remote(*texts: str | None) -> bool:
    return any(t and _REMOTE.search(t) for t in texts)


def is_india_country(country: str | None) -> bool:
    return bool(country) and country.strip().lower() in _INDIA_COUNTRY


def remote_scope(remote: bool, *texts: str | None, country: str | None = None) -> str:
    """'' (not remote) | 'india' | 'global' | 'restricted' (other countries only) | 'unspecified' (bare "Remote")."""
    if not remote:
        return ""
    text = " ; ".join(t for t in texts if t)
    if is_india_country(country) or _REMOTE_INDIA.search(text) or is_india(text):
        return "india"
    if _REMOTE_GLOBAL.search(text):
        return "global"
    if (country and country.strip()) or _REMOTE_RESTRICTED.search(text):
        return "restricted"
    # Any other place named ("Arizona", "Remote - Ontario") is somewhere that isn't India: only a bare "Remote" is unknown.
    if re.search(r"[a-z]", _REMOTE_FILLER.sub(" ", text), re.IGNORECASE):
        return "restricted"
    return "unspecified"


def content_hash(*fields: object) -> str:
    """Stable fingerprint of the fields that affect search. Unchanged hash => skip re-embedding."""
    h = hashlib.sha256()
    for f in fields:
        h.update(str(f if f is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()
