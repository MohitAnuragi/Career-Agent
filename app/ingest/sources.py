"""Fetchers + normalisers for public ATS job-board APIs (no scraping, no auth).

Greenhouse: https://developers.greenhouse.io/job-board.html
Lever:      https://github.com/lever/postings-api
Ashby:      https://developers.ashbyhq.com/docs/public-job-posting-api
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.ingest.text import html_to_text, is_india, is_remote
from app.models import Job

USER_AGENT = "career-agent/1.0 (+https://github.com/MohitAnuragi)"


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- Greenhouse
def greenhouse_url(board: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def normalize_greenhouse(payload: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for j in payload.get("jobs", []):
        location = (j.get("location") or {}).get("name", "") or ""
        offices = " ".join(o.get("location") or o.get("name") or "" for o in j.get("offices") or [])
        dept = ", ".join(d.get("name", "") for d in j.get("departments") or [] if d.get("name"))
        jobs.append(
            Job(
                id=f"greenhouse:{board}:{j['id']}",
                source="greenhouse",
                board=board,
                company=company,  # Greenhouse's company_name is often "X Careers" / "X Job Board"
                title=j.get("title", "").strip(),
                location=location,
                is_remote=is_remote(location),
                is_india=is_india(location, offices),
                department=dept,
                url=j.get("absolute_url", ""),
                description=html_to_text(j.get("content"), escaped=True),
                posted_at=_parse_dt(j.get("first_published") or j.get("updated_at")),
            )
        )
    return jobs


# ---------------------------------------------------------------- Lever
def lever_url(board: str) -> str:
    return f"https://api.lever.co/v0/postings/{board}?mode=json"


def normalize_lever(payload: list, board: str, company: str) -> list[Job]:
    jobs = []
    for j in payload:
        cats = j.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        location = cats.get("location") or ", ".join(all_locs)
        # Lever splits the posting into an intro, titled lists and a closing section;
        # many companies leave descriptionPlain empty and put everything in `lists`.
        sections = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            body = html_to_text(lst.get("content"))
            sections.append(f"{lst.get('text', '')}\n{body}".strip())
        sections.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
        description = "\n\n".join(s for s in sections if s)
        workplace = (j.get("workplaceType") or "").lower()
        jobs.append(
            Job(
                id=f"lever:{board}:{j['id']}",
                source="lever",
                board=board,
                company=company,
                title=(j.get("text") or "").strip(),
                location=location,
                is_remote=workplace == "remote" or is_remote(location),
                is_india=j.get("country") == "IN" or is_india(location, *all_locs),
                employment_type=cats.get("commitment") or "",
                department=", ".join(x for x in (cats.get("department"), cats.get("team")) if x),
                url=j.get("hostedUrl", ""),
                description=description,
                posted_at=_parse_dt(j.get("createdAt")),
            )
        )
    return jobs


# ---------------------------------------------------------------- Ashby
def ashby_url(board: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{board}"


def normalize_ashby(payload: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for j in payload.get("jobs", []):
        if j.get("isListed") is False:
            continue
        location = j.get("location") or ""
        secondary = [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
        jobs.append(
            Job(
                id=f"ashby:{board}:{j['id']}",
                source="ashby",
                board=board,
                company=company,
                title=(j.get("title") or "").strip(),
                location=", ".join([location, *secondary]).strip(", "),
                is_remote=bool(j.get("isRemote"))
                or (j.get("workplaceType") or "").lower() == "remote"
                or is_remote(location),
                is_india=is_india(location, country, *secondary),
                employment_type=j.get("employmentType") or "",
                department=j.get("department") or j.get("team") or "",
                url=j.get("jobUrl", ""),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=_parse_dt(j.get("publishedAt")),
            )
        )
    return jobs


SOURCES: dict[str, tuple[Callable[[str], str], Callable[[Any, str, str], list[Job]]]] = {
    "greenhouse": (greenhouse_url, normalize_greenhouse),
    "lever": (lever_url, normalize_lever),
    "ashby": (ashby_url, normalize_ashby),
}


def fetch_board(client: httpx.Client, source: str, board: str, company: str, retries: int = 2) -> list[Job]:
    url_fn, normalize = SOURCES[source]
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            resp = client.get(url_fn(board))
            resp.raise_for_status()
            return normalize(resp.json(), board, company)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:  # board renamed or removed: retrying won't help
                raise
            last = e
        except (httpx.TransportError, ValueError) as e:
            last = e
    assert last is not None
    raise last


def make_client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
