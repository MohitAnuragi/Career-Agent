"""Find the official public job board (ATS) for each company in a CSV, so boards.json can grow past hand-picked boards.

    python scripts/discover_boards.py data/companies.csv                 # all rows
    python scripts/discover_boards.py data/companies.csv --limit 10      # first 10 rows
    python scripts/discover_boards.py data/companies.csv --no-careers-page

Input CSV columns: company, website, city, discovered_from.

For each company:
  1. Read its own careers page (one request per path, only if its robots.txt allows us) and look for links to a
     supported ATS (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee). Unsupported ATSes
     (Keka, Darwinbox, Zoho Recruit, Freshteam) are reported so you know why a company has no board.
  2. If the page shows nothing, try likely slugs ("razorpay", "razorpay-software", ...) against each ATS's public API.
  3. Verify every candidate through the same fetchers the ingest uses and count total / India / last-14-days jobs.

Writes scripts/out/boards.candidates.json (entries in boards.json format, plus evidence) and
scripts/out/discovery_report.csv. It never edits boards.json: review the candidates and copy the good ones over.
Slug guesses can hit a different company with the same name, so check `evidence` before merging.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ingest.pipeline import BOARDS_FILE, load_boards  # noqa: E402
from app.ingest.sources import SOURCES, USER_AGENT, fetch_board, make_client  # noqa: E402

OUT = ROOT / "scripts" / "out"
CAREERS_PATHS = ["/careers", "/jobs", "/careers/", "/company/careers", "/"]

# Links to supported boards found on a careers page -> (source, slug group).
ATS_LINKS = [
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("recruitee", re.compile(r"//([a-z0-9-]+)\.recruitee\.com")),
]
NOT_SLUGS = {"embed", "api", "j", "v1", "jobs", "js", "static", "assets", "careers", "external-referrals"}
UNSUPPORTED = {
    "keka": re.compile(r"[a-z0-9-]+\.keka\.com", re.I),
    "darwinbox": re.compile(r"darwinbox\.(?:in|com)", re.I),
    "zoho-recruit": re.compile(r"zohorecruit\.(?:com|in)|recruit\.zoho\.", re.I),
    "freshteam": re.compile(r"freshteam\.com", re.I),
}


@dataclass
class Result:
    company: str
    website: str
    city: str
    discovered_from: str
    candidates: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    note: str = ""


def slug_guesses(company: str, website: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", company.lower())
    domain_root = re.sub(r"^www\.", "", website.lower().split("//")[-1].split("/")[0]).split(".")[0]
    guesses = ["".join(words), "-".join(words), domain_root, f"{''.join(words)}hq"]
    return list(dict.fromkeys(g for g in guesses if g))


def careers_page_links(client: httpx.Client, website: str) -> tuple[list[tuple[str, str]], list[str], str]:
    """(supported (source, slug) links, unsupported ATS names, note) from the company's own pages."""
    base = website if website.startswith("http") else f"https://{website}"
    base = base.rstrip("/")
    robots = urllib.robotparser.RobotFileParser()
    try:
        r = client.get(f"{base}/robots.txt", timeout=10)
        robots.parse(r.text.splitlines() if r.status_code == 200 else [])
    except httpx.HTTPError:
        robots.parse([])
    found: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for path in CAREERS_PATHS:
        url = base + path
        if not robots.can_fetch(USER_AGENT, url):
            return found, unsupported, "careers page disallowed by robots.txt"
        try:
            resp = client.get(url, timeout=15)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            continue
        page = resp.text + " " + str(resp.url)  # a redirect straight to the ATS counts too
        for source, pattern in ATS_LINKS:
            for slug in pattern.findall(page):
                if slug.lower() not in NOT_SLUGS and (source, slug) not in found:
                    found.append((source, slug))
        unsupported += [name for name, pat in UNSUPPORTED.items() if pat.search(page) and name not in unsupported]
        if found or unsupported:
            break
    return found, unsupported, ""


def verify(client: httpx.Client, source: str, slug: str, company: str) -> dict | None:
    try:
        jobs = fetch_board(client, source, slug, company, retries=0)
    except Exception:  # 404 / bad payload: not a board
        return None
    if not jobs:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    name = company.lower()
    return {
        "total": len(jobs),
        "india": sum(j.is_india for j in jobs),
        "last_14_days": sum(1 for j in jobs if j.posted_at and j.posted_at >= cutoff),
        "name_in_postings": f"{sum(name in (j.description + ' ' + j.url).lower() for j in jobs)}/{len(jobs)}",
    }


def discover(row: dict, client: httpx.Client, known: set[tuple[str, str]], use_careers_page: bool) -> Result:
    res = Result(row["company"].strip(), row.get("website", "").strip(), row.get("city", "").strip(),
                 row.get("discovered_from", "").strip() or "manual")
    links: list[tuple[str, str]] = []
    if use_careers_page and res.website:
        links, res.unsupported, res.note = careers_page_links(client, res.website)
    via = "careers-page" if links else "slug-guess"
    if not links:
        links = [(source, slug) for slug in slug_guesses(res.company, res.website) for source in SOURCES]
    for source, slug in links:
        if (source, slug) in known:
            res.note = f"already in boards.json as {source}:{slug}"
            continue
        evidence = verify(client, source, slug, res.company)
        if evidence:
            # A guessed slug with no India jobs is most likely a namesake ("Clear", "Slice" exist in the US too).
            evidence["likely_namesake"] = via == "slug-guess" and evidence["india"] == 0
            res.candidates.append({
                "source": source, "board": slug, "company": res.company,
                "discovered_from": res.discovered_from, "added": date.today().isoformat(),
                "evidence": {"via": via, "hq_city": res.city, **evidence},
            })
            if via == "slug-guess":
                break  # first hit is enough; more guesses mostly find namesakes
    return res


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv", type=Path)
    p.add_argument("--limit", type=int, default=0, help="only the first N rows")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--no-careers-page", action="store_true", help="skip careers pages, only probe ATS APIs")
    args = p.parse_args()

    rows = list(csv.DictReader(args.csv.open(encoding="utf-8")))
    rows = rows[: args.limit] if args.limit else rows
    known = {(b["source"], b["board"]) for b in load_boards(BOARDS_FILE)}
    with make_client() as client, ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda r: discover(r, client, known, not args.no_careers_page), rows))

    OUT.mkdir(parents=True, exist_ok=True)
    candidates = [c for r in results for c in r.candidates]
    (OUT / "boards.candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    with (OUT / "discovery_report.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "status", "boards", "india_jobs", "last_14_days", "unsupported_ats", "note"])
        for r in results:
            status = "found" if r.candidates else ("unsupported-ats" if r.unsupported else "not-found")
            if r.candidates and all(c["evidence"]["likely_namesake"] for c in r.candidates):
                status = "check-namesake"
            w.writerow([
                r.company, status, " ".join(f"{c['source']}:{c['board']}" for c in r.candidates),
                sum(c["evidence"]["india"] for c in r.candidates),
                sum(c["evidence"]["last_14_days"] for c in r.candidates),
                " ".join(r.unsupported), r.note,
            ])
    found = sum(1 for r in results if r.candidates)
    print(f"{found}/{len(results)} companies matched to {len(candidates)} boards -> {OUT / 'boards.candidates.json'}")
    print(f"report -> {OUT / 'discovery_report.csv'}")


if __name__ == "__main__":
    main()
