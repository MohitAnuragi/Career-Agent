"""Incremental ingestion: fetch boards -> upsert by content hash -> expire removed jobs -> embed changed jobs.

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
from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import connection
from app.embeddings import embed_passages, job_passage
from app.ingest.sources import fetch_board, make_client
from app.ingest.text import content_hash
from app.models import Job

log = logging.getLogger("ingest")
BOARDS_FILE = Path(__file__).with_name("boards.json")


@dataclass
class IngestStats:
    boards_ok: int = 0
    boards_failed: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    kept: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    expired: int = 0
    embedded: int = 0
    seconds: float = 0.0


def load_boards(path: Path = BOARDS_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def job_hash(j: Job) -> str:
    return content_hash(j.title, j.company, j.location, j.department, j.employment_type, j.is_remote, j.description)


UPSERT = """
INSERT INTO jobs (id, source, board, company, title, location, is_remote, is_india, employment_type,
                  department, url, description, content_hash, posted_at)
VALUES (%(id)s, %(source)s, %(board)s, %(company)s, %(title)s, %(location)s, %(is_remote)s, %(is_india)s,
        %(employment_type)s, %(department)s, %(url)s, %(description)s, %(hash)s, %(posted_at)s)
ON CONFLICT (id) DO UPDATE SET
    company = EXCLUDED.company, title = EXCLUDED.title, location = EXCLUDED.location,
    is_remote = EXCLUDED.is_remote, is_india = EXCLUDED.is_india,
    employment_type = EXCLUDED.employment_type, department = EXCLUDED.department,
    url = EXCLUDED.url, description = EXCLUDED.description, posted_at = EXCLUDED.posted_at,
    -- only a changed posting loses its embedding and gets re-embedded
    embedding = CASE WHEN jobs.content_hash = EXCLUDED.content_hash THEN jobs.embedding END,
    content_hash = EXCLUDED.content_hash,
    last_seen_at = now(),
    is_active = TRUE
"""


def sync_board(conn, source: str, board: str, jobs: list[Job]) -> tuple[int, int, int, int]:
    """Upsert one board's current postings and deactivate ones that disappeared.

    Only called for boards that were fetched successfully, so a network failure never
    mass-expires a company's jobs.
    """
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
        rows.append({**j.model_dump(), "hash": h})
    with conn.cursor() as cur:
        if rows:
            cur.executemany(UPSERT, rows)
        cur.execute(
            """UPDATE jobs SET is_active = FALSE
               WHERE source = %s AND board = %s AND is_active AND NOT (id = ANY(%s))""",
            (source, board, [j.id for j in jobs]),
        )
        expired = cur.rowcount
    return new, changed, unchanged, expired


def embed_pending(conn, batch: int = 128) -> int:
    total = 0
    while True:
        rows = conn.execute(
            """SELECT id, title, company, location, department, description FROM jobs
               WHERE is_active AND embedding IS NULL LIMIT %s""",
            (batch,),
        ).fetchall()
        if not rows:
            return total
        vectors = embed_passages(
            job_passage(r["title"], r["company"], r["location"], r["department"], r["description"]) for r in rows
        )
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE jobs SET embedding = %s WHERE id = %s",
                [(v, r["id"]) for v, r in zip(vectors, rows)],
            )
        conn.commit()
        total += len(rows)
        log.info("embedded %d jobs", total)


def keep(job: Job, all_locations: bool) -> bool:
    return all_locations or job.is_india or job.is_remote


def run_ingest(
    boards: list[dict] | None = None,
    *,
    all_locations: bool = False,
    embed: bool = True,
    db_url: str | None = None,
    workers: int = 8,
) -> IngestStats:
    started = time.perf_counter()
    boards = boards if boards is not None else load_boards()
    stats = IngestStats()

    with make_client() as client, ThreadPoolExecutor(workers) as pool:
        futures = {
            (b["source"], b["board"]): pool.submit(fetch_board, client, b["source"], b["board"], b["company"])
            for b in boards
        }
        fetched: dict[tuple[str, str], list[Job]] = {}
        for key, fut in futures.items():
            try:
                fetched[key] = fut.result()
                stats.boards_ok += 1
            except Exception as e:  # one bad board must not stop the run
                stats.boards_failed[f"{key[0]}:{key[1]}"] = f"{type(e).__name__}: {e}"[:200]
                log.warning("board %s:%s failed: %s", key[0], key[1], e)

    with connection(db_url) as conn:
        run_id = conn.execute("INSERT INTO ingest_runs DEFAULT VALUES RETURNING id").fetchone()["id"]
        for (source, board), jobs in fetched.items():
            stats.fetched += len(jobs)
            jobs = [j for j in jobs if keep(j, all_locations) and j.title and j.url]
            stats.kept += len(jobs)
            n, c, u, e = sync_board(conn, source, board, jobs)
            stats.new += n
            stats.changed += c
            stats.unchanged += u
            stats.expired += e
            conn.commit()
        if embed:
            stats.embedded = embed_pending(conn)
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
    args = p.parse_args()
    stats = run_ingest(all_locations=args.all_locations, embed=not args.no_embed)
    print(json.dumps(asdict(stats), indent=2))


if __name__ == "__main__":
    main()
