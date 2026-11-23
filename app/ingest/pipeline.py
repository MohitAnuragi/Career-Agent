"""Incremental ingestion: fetch boards -> drop old / out-of-scope postings -> upsert by content hash ->
expire removed and aged-out jobs -> embed changed jobs -> prune long-gone jobs.

Run:  python -m app.ingest.pipeline            (all boards in boards.json)
      python -m app.ingest.pipeline --all-locations
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.embeddings import EMBED_VERSION, embed_passages, job_passage
from app.ingest.extract import job_skills, min_years
from app.ingest.sources import fetch_board, make_client
from app.ingest.text import content_hash
from app.models import Job

log = logging.getLogger("ingest")
BOARDS_FILE = Path(__file__).with_name("boards.json")
KEEP_REMOTE = {"india", "global", "unspecified"}  # remote roles someone in India can take

BoardKey = tuple[str, str]  # (source, board)


@dataclass
class IngestStats:
    boards_ok: int = 0
    boards_failed: dict[str, str] = field(default_factory=dict)
    boards_skipped: int = 0  # disabled after repeated failures
    boards_disabled: list[str] = field(default_factory=list)  # disabled by this run
    fetched: int = 0
    kept: int = 0
    too_old: int = 0
    restricted_remote: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    expired: int = 0
    aged_out: int = 0
    embedded: int = 0
    pruned: int = 0
    seconds: float = 0.0


def load_boards(path: Path = BOARDS_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def job_hash(j: Job) -> str:
    return content_hash(j.title, j.company, j.location, j.department, j.employment_type, j.is_remote, j.description)


def drop_reason(job: Job, *, all_locations: bool, cutoff: datetime) -> str | None:
    """Why a fetched posting is not stored, or None to keep it."""
    if not job.title or not job.url:
        return "incomplete"
    if job.posted_at is not None and job.posted_at < cutoff:
        return "too_old"
    if all_locations or job.is_india or job.remote_scope in KEEP_REMOTE:
        return None
    return "restricted_remote" if job.remote_scope == "restricted" else "location"


# effective_posted_at/posted_at_source in VALUES only matter for new rows. For an existing row a missing ATS
# date keeps the stored value, so a re-sync never makes an undated job look new.
UPSERT = """
INSERT INTO jobs (id, source, board, company, title, location, is_remote, is_india, employment_type,
                  department, url, description, content_hash, posted_at, effective_posted_at, posted_at_source,
                  cities, remote_scope, skills, min_years)
VALUES (%(id)s, %(source)s, %(board)s, %(company)s, %(title)s, %(location)s, %(is_remote)s, %(is_india)s,
        %(employment_type)s, %(department)s, %(url)s, %(description)s, %(hash)s, %(posted_at)s,
        %(effective_posted_at)s, %(posted_at_source)s, %(cities)s, %(remote_scope)s, %(skills)s, %(min_years)s)
ON CONFLICT (id) DO UPDATE SET
    company = EXCLUDED.company, title = EXCLUDED.title, location = EXCLUDED.location,
    is_remote = EXCLUDED.is_remote, is_india = EXCLUDED.is_india,
    employment_type = EXCLUDED.employment_type, department = EXCLUDED.department,
    url = EXCLUDED.url, description = EXCLUDED.description, posted_at = EXCLUDED.posted_at,
    effective_posted_at = COALESCE(EXCLUDED.posted_at, jobs.effective_posted_at),
    posted_at_source = CASE WHEN EXCLUDED.posted_at IS NOT NULL THEN 'ats' ELSE jobs.posted_at_source END,
    cities = EXCLUDED.cities, remote_scope = EXCLUDED.remote_scope,
    skills = EXCLUDED.skills, min_years = EXCLUDED.min_years,
    -- only a changed posting loses its embedding and gets re-embedded
    embedding = CASE WHEN jobs.content_hash = EXCLUDED.content_hash THEN jobs.embedding END,
    content_hash = EXCLUDED.content_hash,
    last_seen_at = now(),
    is_active = TRUE
"""


def derived(title: str, description: str) -> dict:
    """Fields computed from a posting's text at ingest (see app/ingest/extract.py)."""
    return {"skills": job_skills(title, description), "min_years": min_years(description)}


def board_known(conn, source: str, board: str) -> bool:
    return conn.execute("SELECT 1 FROM boards WHERE source = %s AND board = %s", (source, board)).fetchone() is not None


def sync_board(conn, source: str, board: str, jobs: list[Job], company: str | None = None) -> tuple[int, int, int, int]:
    """Upsert one board's current postings and deactivate ones that disappeared.

    Only called for boards that were fetched successfully, so a network failure never
    mass-expires a company's jobs.
    """
    # On a board's first ingest every job is "first seen" now, so first_seen_at says nothing about its age.
    known = board_known(conn, source, board)
    now = datetime.now(timezone.utc)
    existing = {
        r["id"]: r["content_hash"]
        for r in conn.execute(
            "SELECT id, content_hash FROM jobs WHERE source = %s AND board = %s", (source, board)
        )
    }
    rows, new, changed, unchanged = [], 0, 0, 0
    for j in jobs:
        h = job_hash(j)
        if j.id not in existing:
            new += 1
        elif existing[j.id] != h:
            changed += 1
        else:
            unchanged += 1
        if j.posted_at is not None:
            effective, src = j.posted_at, "ats"
        elif known:
            effective, src = now, "first_seen"
        else:
            effective, src = None, "unknown"
        rows.append({**j.model_dump(), **derived(j.title, j.description), "hash": h,
                     "effective_posted_at": effective, "posted_at_source": src})
    with conn.cursor() as cur:
        if rows:
            cur.executemany(UPSERT, rows)
        cur.execute(
            """UPDATE jobs SET is_active = FALSE
               WHERE source = %s AND board = %s AND is_active AND NOT (id = ANY(%s))""",
            (source, board, [j.id for j in jobs]),
        )
        expired = cur.rowcount
        cur.execute(
            """INSERT INTO boards (source, board, company, last_ok_at) VALUES (%s, %s, %s, now())
               ON CONFLICT (source, board) DO UPDATE SET company = EXCLUDED.company, last_ok_at = now(),
                   last_error = NULL, consecutive_failures = 0""",
            (source, board, company or (jobs[0].company if jobs else board)),
        )
    return new, changed, unchanged, expired


def record_board_failure(conn, source: str, board: str, company: str, error: str) -> bool:
    """Count a failed fetch; returns True when this failure disabled the board."""
    max_failures = get_settings().board_max_failures
    row = conn.execute(
        """INSERT INTO boards (source, board, company, first_ingested_at, last_error, consecutive_failures)
           VALUES (%s, %s, %s, now(), %s, 1)
           ON CONFLICT (source, board) DO UPDATE SET last_error = EXCLUDED.last_error,
               consecutive_failures = boards.consecutive_failures + 1
           RETURNING consecutive_failures, disabled""",
        (source, board, company, error),
    ).fetchone()
    if row["consecutive_failures"] >= max_failures and not row["disabled"]:
        conn.execute("UPDATE boards SET disabled = TRUE WHERE source = %s AND board = %s", (source, board))
        return True
    return False


def disabled_boards(conn) -> set[BoardKey]:
    return {(r["source"], r["board"]) for r in conn.execute("SELECT source, board FROM boards WHERE disabled")}


def age_out(conn, max_age_days: int) -> int:
    """Deactivate jobs older than the window, including undated ones (aged by first_seen_at)."""
    cur = conn.execute(
        """UPDATE jobs SET is_active = FALSE
           WHERE is_active AND COALESCE(effective_posted_at, first_seen_at) < now() - make_interval(days => %s::int)""",
        (max_age_days,),
    )
    return cur.rowcount


def prune(conn, after_days: int) -> int:
    """Delete long-inactive jobs. Jobs with feedback are kept: deleting would cascade away a user's saved job."""
    cur = conn.execute(
        """DELETE FROM jobs j
           WHERE NOT j.is_active AND j.last_seen_at < now() - make_interval(days => %s::int)
             AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.job_id = j.id)""",
        (after_days,),
    )
    return cur.rowcount


def embed_pending(conn, batch: int = 128) -> int:
    """Embed active jobs that have no embedding, or one made with an older EMBED_VERSION."""
    total = 0
    while True:
        rows = conn.execute(
            """SELECT id, title, company, location, department, description FROM jobs
               WHERE is_active AND (embedding IS NULL OR embedding_version < %s) LIMIT %s""",
            (EMBED_VERSION, batch),
        ).fetchall()
        if not rows:
            return total
        vectors = embed_passages(
            job_passage(r["title"], r["company"], r["location"], r["department"], r["description"]) for r in rows
        )
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE jobs SET embedding = %s, embedding_version = %s WHERE id = %s",
                [(v, EMBED_VERSION, r["id"]) for v, r in zip(vectors, rows)],
            )
        conn.commit()
        total += len(rows)
        log.info("embedded %d jobs", total)


def reprocess(conn, *, embed: bool = True, batch: int = 500) -> dict:
    """Recompute derived fields for every stored job and re-embed outdated ones, without fetching boards.

    Ingest keeps these current for jobs still on their boards; this covers the rest (saved jobs that went
    inactive, the eval snapshot) after extract.py or EMBED_VERSION changes.
    """
    updated, last = 0, ""
    while True:
        rows = conn.execute(
            "SELECT id, title, description FROM jobs WHERE id > %s ORDER BY id LIMIT %s", (last, batch)
        ).fetchall()
        if not rows:
            break
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE jobs SET skills = %(skills)s, min_years = %(min_years)s WHERE id = %(id)s",
                [{"id": r["id"], **derived(r["title"], r["description"])} for r in rows],
            )
        conn.commit()
        updated += len(rows)
        last = rows[-1]["id"]
    return {"derived": updated, "embedded": embed_pending(conn) if embed else 0}


def store_results(
    conn,
    fetched: dict[BoardKey, tuple[str, list[Job]]],
    failed: dict[BoardKey, tuple[str, str]],
    *,
    all_locations: bool = False,
    embed: bool = True,
    stats: IngestStats | None = None,
) -> IngestStats:
    """The database half of a run: sync fetched boards, record failures, expire, embed, prune."""
    s = get_settings()
    stats = stats or IngestStats()
    cutoff = datetime.now(timezone.utc) - timedelta(days=s.ingest_max_age_days)
    for (source, board), (company, error) in failed.items():
        stats.boards_failed[f"{source}:{board}"] = error
        if record_board_failure(conn, source, board, company, error):
            stats.boards_disabled.append(f"{source}:{board}")
    conn.commit()
    for (source, board), (company, jobs) in fetched.items():
        stats.boards_ok += 1
        stats.fetched += len(jobs)
        kept = []
        for j in jobs:
            reason = drop_reason(j, all_locations=all_locations, cutoff=cutoff)
            if reason is None:
                kept.append(j)
            elif reason == "too_old":
                stats.too_old += 1
            elif reason == "restricted_remote":
                stats.restricted_remote += 1
        stats.kept += len(kept)
        n, c, u, e = sync_board(conn, source, board, kept, company)
        stats.new += n
        stats.changed += c
        stats.unchanged += u
        stats.expired += e
        conn.commit()
    stats.aged_out = age_out(conn, s.ingest_max_age_days)
    conn.commit()
    if embed:
        stats.embedded = embed_pending(conn)
    stats.pruned = prune(conn, s.prune_after_days)
    conn.commit()
    return stats


def run_ingest(
    boards: list[dict] | None = None,
    *,
    all_locations: bool = False,
    embed: bool = True,
    db_url: str | None = None,
    workers: int = 16,
) -> IngestStats:
    started = time.perf_counter()
    boards = boards if boards is not None else load_boards()
    stats = IngestStats()

    with connection(db_url) as conn:  # short-lived: don't hold a pooled connection during the fetch
        skip = disabled_boards(conn)
    todo = [b for b in boards if (b["source"], b["board"]) not in skip]
    stats.boards_skipped = len(boards) - len(todo)

    fetched: dict[BoardKey, tuple[str, list[Job]]] = {}
    failed: dict[BoardKey, tuple[str, str]] = {}
    with make_client() as client, ThreadPoolExecutor(workers) as pool:
        futures = {
            (b["source"], b["board"]): (b["company"], pool.submit(fetch_board, client, b["source"], b["board"], b["company"]))
            for b in todo
        }
        for key, (company, fut) in futures.items():
            try:
                fetched[key] = (company, fut.result())
            except Exception as e:  # one bad board must not stop the run
                failed[key] = (company, f"{type(e).__name__}: {e}"[:200])
                log.warning("board %s:%s failed: %s", key[0], key[1], e)

    with connection(db_url) as conn:
        run_id = conn.execute("INSERT INTO ingest_runs DEFAULT VALUES RETURNING id").fetchone()["id"]
        conn.commit()
        store_results(conn, fetched, failed, all_locations=all_locations, embed=embed, stats=stats)
        stats.seconds = round(time.perf_counter() - started, 1)
        conn.execute(
            "UPDATE ingest_runs SET finished_at = now(), stats = %s WHERE id = %s",
            (Jsonb(asdict(stats)), run_id),
        )
        conn.commit()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="Ingest jobs from public ATS boards")
    p.add_argument("--all-locations", action="store_true", help="keep jobs outside India / non-remote too")
    p.add_argument("--no-embed", action="store_true", help="skip the embedding step")
    p.add_argument("--reprocess", action="store_true",
                   help="don't fetch: recompute derived fields and re-embed outdated jobs already stored")
    p.add_argument("--db-url", help="database to use (default: DATABASE_URL)")
    args = p.parse_args()
    if args.reprocess:
        with connection(args.db_url) as conn:
            print(json.dumps(reprocess(conn, embed=not args.no_embed), indent=2))
        return
    stats = run_ingest(all_locations=args.all_locations, embed=not args.no_embed, db_url=args.db_url)
    print(json.dumps(asdict(stats), indent=2))


if __name__ == "__main__":
    main()
