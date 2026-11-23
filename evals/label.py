"""Pooled relevance labelling with an LLM judge (TREC-style pooling).

For each query, take the union of the top-K results from every retrieval method, then grade each pooled
job 0-3. Unpooled jobs are treated as non-relevant by the metrics.

After a ranking change, run with --extend: existing grades are kept and only newly retrieved jobs are graded,
so a new method isn't penalised for finding jobs nobody judged.
"""
from __future__ import annotations

import argparse
import time
from datetime import date

from pydantic import BaseModel, Field

from app import search
from app.config import get_settings
from app.db import connection
from app.llm.base import LLMError
from app.models import SearchFilters
from evals.common import QRELS, ROOT, eval_db_url, judge_llm, load_json, write_json

RUBRIC = """Grade how well each job posting matches the search query.
3 = exactly the role (and level, if the query states one)
2 = clearly relevant; a reasonable person running this query would want to see it
1 = tangentially related (same field, wrong role or level)
0 = not relevant
Judge from the title and description only. Ignore location; it is filtered separately."""


class Grade(BaseModel):
    job_id: str
    grade: int = Field(ge=0, le=3)


class Grades(BaseModel):
    items: list[Grade]


def pool_for(conn, q: dict, depth: int) -> list:
    filters = SearchFilters(india_only=q.get("india_only", False))
    pooled: dict[str, object] = {}
    for method in ("keyword", "vector", "hybrid"):
        for r in search.search(conn, q["query"], method=method, filters=filters, limit=depth):
            pooled.setdefault(r.job.id, r.job)
    return list(pooled.values())


def judge(llm, prompt: str, attempts: int = 4) -> Grades:
    """One judge call, retried on top of the client's own backoff (the free tier has long 503 spells)."""
    for n in range(attempts):
        try:
            return llm.structured(prompt, Grades, system=RUBRIC)
        except LLMError:
            if n == attempts - 1:
                raise
            time.sleep(30 * (n + 1))
    raise AssertionError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=20, help="top-K per method to pool")
    ap.add_argument("--batch", type=int, default=12, help="jobs per judge call")
    ap.add_argument("--extend", action="store_true",
                    help="keep existing labels and grade only newly pooled jobs (after a ranking change)")
    args = ap.parse_args()

    llm = judge_llm()
    queries = load_json(ROOT / "queries.json")
    old = load_json(QRELS) if args.extend and QRELS.exists() else {}
    qrels: dict[str, dict[str, int]] = {k: dict(v) for k, v in old.get("qrels", {}).items()}
    s = get_settings()
    meta = {
        "judge": s.judge_model or s.gemini_model,
        "created": date.today().isoformat(),
        "pool_depth": args.depth,
        "human_checked": 0,
        "human_agreement": None,
    }
    if args.extend and old:  # keep the original metadata; note when the pool was last extended
        meta.update({k: v for k, v in old.items() if k != "qrels"}, extended=date.today().isoformat())

    added = 0
    with connection(eval_db_url()) as conn:
        for q in queries:
            grades = qrels.setdefault(q["id"], {})
            jobs = [j for j in pool_for(conn, q, args.depth) if j.id not in grades]
            for i in range(0, len(jobs), args.batch):
                chunk = jobs[i : i + args.batch]
                listing = "\n\n".join(f"[{j.id}] {j.title} | {j.company}\n{j.description[:900]}" for j in chunk)
                res = judge(llm, f"<query>{q['query']}</query>\n<jobs>\n{listing}\n</jobs>")
                valid = {j.id for j in chunk}
                new = {g.job_id: g.grade for g in res.items if g.job_id in valid}
                grades.update(new)
                added += len(new)
            # Saved after every query: a later failure loses nothing, and --extend resumes from here.
            write_json(QRELS, {**meta, "qrels": qrels})
            print(f"{q['id']}: graded {len(jobs)} new, relevant(>=2) {sum(g >= 2 for g in grades.values())}")
    print(f"wrote {QRELS} ({added} new judgements)")


if __name__ == "__main__":
    main()
