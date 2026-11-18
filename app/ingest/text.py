"""Text cleanup and location classification for raw job-board payloads."""
from __future__ import annotations

import hashlib
import html
import re

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "blockquote"]

# Word boundaries matter: "Indiana" and "Indianapolis" are not India.
_INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|gurugram|gurgaon|mumbai|pune|hyderabad|chennai|noida|"
    r"new delhi|delhi|ncr|kolkata|ahmedabad|jaipur|kochi|chandigarh|indore|coimbatore)\b",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.IGNORECASE)


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


def content_hash(*fields: object) -> str:
    """Stable fingerprint of the fields that affect search. Unchanged hash => skip re-embedding."""
    h = hashlib.sha256()
    for f in fields:
        h.update(str(f if f is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()
