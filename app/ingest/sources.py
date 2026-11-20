"""Fetchers + normalisers for public ATS job-board APIs (no scraping, no auth).

Greenhouse:      https://developers.greenhouse.io/job-board.html
Lever:           https://github.com/lever/postings-api
Ashby:           https://developers.ashbyhq.com/docs/public-job-posting-api
Workable:        https://apply.workable.com/api/v1/widget/accounts/{slug}  (public careers widget feed)
SmartRecruiters: https://developers.smartrecruiters.com/docs/posting-api
Recruitee:       https://{slug}.recruitee.com/api/offers/  (public careers site API)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.ingest.cities import extract_cities
from app.ingest.text import html_to_text, is_india, is_india_country, is_remote, remote_scope
from app.models import Job

USER_AGENT = "career-agent/1.0 (+https://github.com/MohitAnuragi)"


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if text.endswith(" UTC"):  # Recruitee: "2026-11-18 16:23:32 UTC"
        text = text[:-4] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # Workable: date only


def _geo(*texts: str | None, country: str | None = None, remote: bool = False) -> dict:
    """Location fields shared by every source: cities, India flag, remote flag and remote scope."""
    cities = extract_cities(*texts)
    remote = remote or is_remote(*texts)
    return {
        "cities": cities,
        "is_india": bool(cities) or is_india_country(country) or is_india(*texts),
        "is_remote": remote,
        "remote_scope": remote_scope(remote, *texts, country=country),
    }


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
                **_geo(location, offices),
                department=dept,
                url=j.get("absolute_url", ""),
                description=html_to_text(j.get("content"), escaped=True),
                # Not updated_at: any edit bumps it, which would make an old posting look new.
                posted_at=_parse_dt(j.get("first_published")),
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
                **_geo(location, *all_locs, country=j.get("country"), remote=workplace == "remote"),
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
        remote = bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote"
        jobs.append(
            Job(
                id=f"ashby:{board}:{j['id']}",
                source="ashby",
                board=board,
                company=company,
                title=(j.get("title") or "").strip(),
                location=", ".join([location, *secondary]).strip(", "),
                **_geo(location, *secondary, country=country, remote=remote),
                employment_type=j.get("employmentType") or "",
                department=j.get("department") or j.get("team") or "",
                url=j.get("jobUrl", ""),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=_parse_dt(j.get("publishedAt")),
            )
        )
    return jobs


# ---------------------------------------------------------------- Workable
def workable_url(board: str) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{board}?details=true"


def normalize_workable(payload: dict, board: str, company: str) -> list[Job]:
    # The feed repeats a job once per location under the same shortcode: merge them into one posting.
    groups: dict[str, list[dict]] = {}
    for j in payload.get("jobs", []):
        key = j.get("shortcode") or j.get("url") or ""
        if key:
            groups.setdefault(key, []).append(j)
    jobs = []
    for code, entries in groups.items():
        first = entries[0]
        places: list[str] = []
        countries: list[str] = []
        for e in entries:
            locs = e.get("locations") or [{"city": e.get("city"), "region": e.get("state"), "country": e.get("country")}]
            for loc in locs:
                if loc.get("hidden"):
                    continue
                text = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
                if text and text not in places:
                    places.append(text)
                countries.append(loc.get("countryCode") or loc.get("country") or "")
        remote = any(e.get("telecommuting") for e in entries)
        location = "; ".join(places)
        if remote:
            location = f"Remote - {location}" if location else "Remote"
        country = next((c for c in countries if is_india_country(c)), next((c for c in countries if c), ""))
        jobs.append(
            Job(
                id=f"workable:{board}:{code}",
                source="workable",
                board=board,
                company=company,
                title=(first.get("title") or "").strip(),
                location=location,
                **_geo(*places, country=country, remote=remote),
                employment_type=first.get("employment_type") or "",
                department=first.get("department") or first.get("function") or "",
                url=first.get("url") or first.get("shortlink") or "",
                description=html_to_text(first.get("description")),
                posted_at=_parse_dt(first.get("published_on")),
            )
        )
    return jobs


# ---------------------------------------------------------------- Recruitee
def recruitee_url(board: str) -> str:
    return f"https://{board}.recruitee.com/api/offers/"


def normalize_recruitee(payload: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for o in payload.get("offers", []):
        if o.get("status") not in (None, "published"):
            continue
        places = [
            ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x)
            for loc in o.get("locations") or []
        ] or [o.get("location") or ""]
        places = [p for p in places if p]
        description = "\n\n".join(
            t for t in (html_to_text(o.get("description")), html_to_text(o.get("requirements"))) if t
        )
        jobs.append(
            Job(
                id=f"recruitee:{board}:{o['id']}",
                source="recruitee",
                board=board,
                company=company,
                title=(o.get("title") or "").strip(),
                location="; ".join(places),
                **_geo(*places, country=o.get("country_code"), remote=bool(o.get("remote"))),
                employment_type=o.get("employment_type_code") or "",
                department=o.get("department") or "",
                url=o.get("careers_url") or "",
                description=description,
                posted_at=_parse_dt(o.get("published_at")),
            )
        )
    return jobs


# ---------------------------------------------------------------- SmartRecruiters
SR_PAGE = 100
SR_MAX_POSTINGS = 1000  # per board and run; large employers list thousands


def smartrecruiters_url(board: str) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{board}/postings"


def normalize_smartrecruiters(postings: list[dict], board: str, company: str) -> list[Job]:
    """`postings` are posting *detail* objects (GET .../postings/{id}); the list endpoint has no description."""
    jobs = []
    for p in postings:
        loc = p.get("location") or {}
        location = ", ".join(x.strip() for x in (loc.get("fullLocation") or "").split(",") if x.strip())
        sections = (p.get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(
            t for t in (html_to_text((sections.get(k) or {}).get("text"))
                        for k in ("jobDescription", "qualifications", "additionalInformation")) if t
        )
        jobs.append(
            Job(
                id=f"smartrecruiters:{board}:{p['id']}",
                source="smartrecruiters",
                board=board,
                company=company,
                title=(p.get("name") or "").strip(),
                location=location,
                **_geo(location, loc.get("city"), country=loc.get("country"), remote=bool(loc.get("remote"))),
                employment_type=(p.get("typeOfEmployment") or {}).get("label") or "",
                department=(p.get("department") or {}).get("label") or (p.get("function") or {}).get("label") or "",
                url=p.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{p['id']}",
                description=description,
                posted_at=_parse_dt(p.get("releasedDate")),
            )
        )
    return jobs


def fetch_smartrecruiters(client: httpx.Client, board: str, company: str) -> list[Job]:
    """India postings only (`country=in`): remote postings elsewhere always carry a non-India country, so
    they'd be dropped as 'restricted' anyway. Details are fetched only for postings inside the age window."""
    base = smartrecruiters_url(board)
    cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().ingest_max_age_days)
    listed: list[dict] = []
    offset = 0
    while offset < SR_MAX_POSTINGS:
        page = get_json(client, base, params={"country": "in", "limit": SR_PAGE, "offset": offset})
        content = page.get("content") or []
        listed += content
        offset += SR_PAGE
        if not content or offset >= (page.get("totalFound") or 0):
            break
    fresh = [p for p in listed if (dt := _parse_dt(p.get("releasedDate"))) is None or dt >= cutoff]
    details = [get_json(client, f"{base}/{p['id']}") for p in fresh]
    return normalize_smartrecruiters(details, board, company)


# ---------------------------------------------------------------- registry
@dataclass(frozen=True)
class Source:
    url: Callable[[str], str]
    normalize: Callable[[Any, str, str], list[Job]]
    fetch: Callable[[httpx.Client, str, str], list[Job]] | None = None  # for multi-request sources


SOURCES: dict[str, Source] = {
    "greenhouse": Source(greenhouse_url, normalize_greenhouse),
    "lever": Source(lever_url, normalize_lever),
    "ashby": Source(ashby_url, normalize_ashby),
    "workable": Source(workable_url, normalize_workable),
    "recruitee": Source(recruitee_url, normalize_recruitee),
    "smartrecruiters": Source(smartrecruiters_url, normalize_smartrecruiters, fetch_smartrecruiters),
}


# ---------------------------------------------------------------- HTTP
HOST_CONCURRENCY = 4  # requests in flight per ATS, across all boards
MAX_RETRY_AFTER = 30.0
_host_sems: dict[str, threading.BoundedSemaphore] = {}
_host_lock = threading.Lock()


def _host_key(url: str) -> str:
    # Per-company subdomains (acme.recruitee.com) share one ATS, so limit by registered domain.
    return ".".join(httpx.URL(url).host.split(".")[-2:])


def _host_sem(url: str) -> threading.BoundedSemaphore:
    key = _host_key(url)
    with _host_lock:
        if key not in _host_sems:
            _host_sems[key] = threading.BoundedSemaphore(HOST_CONCURRENCY)
        return _host_sems[key]


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(MAX_RETRY_AFTER, max(1.0, float(resp.headers.get("Retry-After", ""))))
    except ValueError:
        return 5.0


def get_json(client: httpx.Client, url: str, params: dict | None = None) -> Any:
    with _host_sem(url):
        resp = client.get(url, params=params)
    if resp.status_code in (429, 503):  # back off here; the caller's retry loop tries again
        time.sleep(_retry_after(resp))
    resp.raise_for_status()
    return resp.json()


def fetch_board(client: httpx.Client, source: str, board: str, company: str, retries: int = 2) -> list[Job]:
    src = SOURCES[source]
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            if src.fetch is not None:
                return src.fetch(client, board, company)
            return src.normalize(get_json(client, src.url(board)), board, company)
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
