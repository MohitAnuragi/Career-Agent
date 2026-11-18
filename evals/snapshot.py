"""Freeze the live index for reproducible evals.

python -m evals.snapshot export   live DB -> evals/data/snapshot.jsonl.gz -> eval DB
python -m evals.snapshot load     evals/data/snapshot.jsonl.gz -> eval DB
"""
from __future__ import annotations

import base64
import gzip
import json
import sys

import numpy as np

from app.db import connection
from evals.common import SNAPSHOT, eval_db_url

COLS = ["id", "source", "board", "company", "title", "location", "is_remote", "is_india", "employment_type",
        "department", "url", "description", "content_hash", "posted_at"]


def export() -> int:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with connection() as conn, gzip.open(SNAPSHOT, "wt", encoding="utf-8") as out:
        rows = conn.execute(
            f"SELECT {', '.join(COLS)}, embedding FROM jobs WHERE is_active AND embedding IS NOT NULL ORDER BY id"
        )
        for r in rows:
            raw = r.pop("embedding")  # pgvector.Vector
            vec = np.asarray(raw.to_numpy() if hasattr(raw, "to_numpy") else raw, dtype=np.float32)
            r["embedding_b64"] = base64.b64encode(vec.tobytes()).decode()
            out.write(json.dumps(r, default=str) + "\n")
            n += 1
    print(f"exported {n} jobs -> {SNAPSHOT}")
    return n


def load() -> int:
    url = eval_db_url()
    rows = []
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            r["embedding"] = np.frombuffer(base64.b64decode(r.pop("embedding_b64")), dtype=np.float32)
            rows.append(r)
    with connection(url) as conn:
        conn.execute("TRUNCATE jobs CASCADE")
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO jobs ({', '.join(COLS)}, embedding) VALUES "
                f"({', '.join('%(' + c + ')s' for c in COLS)}, %(embedding)s)",
                rows,
            )
        conn.commit()
    print(f"loaded {len(rows)} jobs into {url.rsplit('/', 1)[-1]}")
    return len(rows)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "export"
    if cmd == "export":
        export()
        load()
    elif cmd == "load":
        load()
    else:
        sys.exit("usage: python -m evals.snapshot [export|load]")
