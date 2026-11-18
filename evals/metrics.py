"""Standard IR metrics over graded relevance labels (qrels: {doc_id: grade}, grade 0-3)."""
from __future__ import annotations

import math
from statistics import mean


def dcg(gains: list[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: list[str], qrels: dict[str, int], k: int = 10) -> float:
    gains = [qrels.get(d, 0) for d in ranked[:k]]
    ideal = sorted(qrels.values(), reverse=True)[:k]
    best = dcg(ideal)
    return dcg(gains) / best if best > 0 else 0.0


def relevant(qrels: dict[str, int], min_grade: int = 2) -> set[str]:
    return {d for d, g in qrels.items() if g >= min_grade}


def recall_at_k(ranked: list[str], qrels: dict[str, int], k: int = 20, min_grade: int = 2) -> float:
    rel = relevant(qrels, min_grade)
    if not rel:
        return 0.0
    return len(rel & set(ranked[:k])) / len(rel)


def precision_at_k(ranked: list[str], qrels: dict[str, int], k: int = 10, min_grade: int = 2) -> float:
    if k == 0:
        return 0.0
    rel = relevant(qrels, min_grade)
    return sum(1 for d in ranked[:k] if d in rel) / k


def mrr(ranked: list[str], qrels: dict[str, int], min_grade: int = 2) -> float:
    rel = relevant(qrels, min_grade)
    for i, d in enumerate(ranked, start=1):
        if d in rel:
            return 1.0 / i
    return 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[idx]


def summarise(per_query: list[dict[str, float]]) -> dict[str, float]:
    keys = per_query[0].keys() if per_query else []
    return {k: round(mean(q[k] for q in per_query), 4) for k in keys}
