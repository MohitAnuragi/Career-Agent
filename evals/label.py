"""Pooled relevance labelling with an LLM judge (TREC-style pooling).

For each query, take the union of the top-K results from every retrieval method, then grade each pooled
job 0-3. Unpooled jobs are treated as non-relevant by the metrics.
"""
from __future__ import annotations

import argparse
from datetime import date

from pydantic import BaseModel, Field

from app import search
from app.config import get_settings
from app.db import connection
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=20, help="top-K per method to pool")
    ap.add_argument("--batch", type=int, default=12, help="jobs per judge call")
    args = ap.parse_args()

    llm = judge_llm()
    queries = load_json(ROOT / "queries.json")
    qrels: dict[str, dict[str, int]] = {}
    with connection(eval_db_url()) as conn:
        for q in queries:
            jobs = pool_for(conn, q, args.depth)
            grades: dict[str, int] = {}
            for i in range(0, len(jobs), args.batch):
                chunk = jobs[i : i + args.batch]
                listing = "\n\n".join(f"[{j.id}] {j.title} | {j.company}\n{j.description[:900]}" for j in chunk)
                res = llm.structured(f"<query>{q['query']}</query>\n<jobs>\n{listing}\n</jobs>", Grades, system=RUBRIC)
                valid = {j.id for j in chunk}
                grades.update({g.job_id: g.grade for g in res.items if g.job_id in valid})
            qrels[q["id"]] = grades
            print(f"{q['id']}: pooled {len(jobs)}, relevant(>=2) {sum(g >= 2 for g in grades.values())}")

    s = get_settings()
    write_json(QRELS, {
        "judge": s.judge_model or s.gemini_model,
        "created": date.today().isoformat(),
        "pool_depth": args.depth,
        "human_checked": 0,
        "human_agreement": None,
        "qrels": qrels,
    })
    print(f"wrote {QRELS}")


if __name__ == "__main__":
    main()
