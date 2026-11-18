"""Compare retrieval methods on the frozen snapshot.  python -m evals.run_retrieval [--no-rerank]"""
from __future__ import annotations

import argparse
import time

from app import search
from app.db import connection
from app.models import SearchFilters
from evals.common import QRELS, RESULTS, ROOT, eval_db_url, load_json, write_json
from evals.metrics import mrr, ndcg_at_k, percentile, recall_at_k, summarise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rerank", action="store_true", help="skip the LLM reranker (no API calls)")
    args = ap.parse_args()

    queries = load_json(ROOT / "queries.json")
    labels = load_json(QRELS)
    qrels = labels["qrels"]
    methods = [m for m in search.METHODS if not (args.no_rerank and m == "hybrid_rerank")]
    llm = None
    if "hybrid_rerank" in methods:
        from app.llm import get_llm

        llm = get_llm()

    report: dict[str, dict] = {}
    with connection(eval_db_url()) as conn:
        search.embed_query("warm up")  # exclude model load from latency
        for method in methods:
            per_q, latencies = [], []
            for q in queries:
                if q["id"] not in qrels:
                    continue
                t0 = time.perf_counter()
                results = search.search(conn, q["query"], method=method, limit=20, llm=llm,
                                        filters=SearchFilters(india_only=q.get("india_only", False)))
                latencies.append((time.perf_counter() - t0) * 1000)
                ranked = [r.job.id for r in results]
                gold = qrels[q["id"]]
                per_q.append({"ndcg@10": ndcg_at_k(ranked, gold, 10), "recall@20": recall_at_k(ranked, gold, 20),
                              "mrr": mrr(ranked, gold)})
            report[method] = {**summarise(per_q), "p50_ms": round(percentile(latencies, 50)),
                              "p95_ms": round(percentile(latencies, 95)), "queries": len(per_q)}

    out = {"judge": labels.get("judge"), "created": labels.get("created"), "results": report}
    write_json(RESULTS / "retrieval.json", out)

    print(f"\nQueries: {next(iter(report.values()))['queries']}  |  judge: {labels.get('judge')}\n")
    print("| Method | nDCG@10 | Recall@20 | MRR | p50 ms | p95 ms |")
    print("|---|---|---|---|---|---|")
    for m, r in report.items():
        print(f"| {m} | {r['ndcg@10']:.3f} | {r['recall@20']:.3f} | {r['mrr']:.3f} | {r['p50_ms']} | {r['p95_ms']} |")


if __name__ == "__main__":
    main()
