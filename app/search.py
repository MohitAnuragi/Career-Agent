"""Retrieval and ranking.

    candidates:  Postgres full-text (OR query, ts_rank_cd)   +   pgvector cosine (HNSW)
    fusion:      Reciprocal Rank Fusion  score = sum 1 / (k + rank)
    personalise: preference boosts, dismissed jobs / avoided companies filtered out
    rerank:      optional LLM pass over the top N with structured output + per-job reason
"""
from __future__ import annotations

import re
from typing import Literal

from app.config import get_settings
from app.embeddings import embed_query
from app.llm.base import LLM
from app.models import CandidateProfile, Job, Preferences, RerankResult, ScoredJob, SearchFilters

Method = Literal["keyword", "vector", "hybrid", "hybrid_rerank"]
METHODS: tuple[Method, ...] = ("keyword", "vector", "hybrid", "hybrid_rerank")

JOB_COLS = (
    "id, source, board, company, title, location, is_remote, is_india, employment_type, "
    "department, url, description, posted_at"
)
_TOKEN = re.compile(r"[a-z0-9]+")

# Postings use old and new city names interchangeably ("Bangalore" vs "Bengaluru").
CITY_ALIASES = [
    {"bengaluru", "bangalore"},
    {"gurugram", "gurgaon"},
    {"mumbai", "bombay"},
    {"delhi", "new delhi", "ncr"},
    {"chennai", "madras"},
    {"kolkata", "calcutta"},
]
# A typed query asking for early-career roles ("internships in Pune", "new grad roles").
_EARLY_QUERY = re.compile(r"\b(interns?|internships?|new grads?|graduates?|freshers?|entry[- ]level|trainees?)\b", re.I)


def location_patterns(location: str) -> list[str]:
    loc = location.lower().strip()
    for group in CITY_ALIASES:
        if loc in group:
            return [f"%{name}%" for name in sorted(group)]
    return [f"%{loc}%"]


def is_early_career_query(query: str) -> bool:
    return bool(_EARLY_QUERY.search(query))


# ---------------------------------------------------------------- SQL helpers
def _where(f: SearchFilters) -> tuple[str, list]:
    clauses, params = ["is_active"], []
    if f.india_only:
        clauses.append("is_india")
    if f.remote_only:
        clauses.append("is_remote")
    if f.location:
        clauses.append("location ILIKE ANY(%s)")
        params.append(location_patterns(f.location))
    if f.company:
        clauses.append("company ILIKE %s")
        params.append(f"%{f.company}%")
    if f.exclude_job_ids:
        clauses.append("NOT (id = ANY(%s))")
        params.append(f.exclude_job_ids)
    if f.exclude_companies:
        clauses.append("NOT (lower(company) = ANY(%s))")
        params.append([c.lower() for c in f.exclude_companies])
    if f.title_regex:
        clauses.append("title ~* %s")
        params.append(f.title_regex)
    return " AND ".join(clauses), params


def or_tsquery(text: str, max_terms: int = 40) -> str:
    """'Senior Python/FastAPI dev' -> 'senior | python | fastapi | dev' (to_tsquery stems and drops stopwords)."""
    seen: list[str] = []
    for t in _TOKEN.findall(text.lower()):
        if t not in seen:
            seen.append(t)
    return " | ".join(seen[:max_terms])


def keyword_search(conn, query: str, filters: SearchFilters, limit: int) -> list[str]:
    tsq = or_tsquery(query)
    if not tsq:
        return []
    where, params = _where(filters)
    rows = conn.execute(
        f"""SELECT id FROM jobs, to_tsquery('english', %s) q
            WHERE search_tsv @@ q AND {where}
            ORDER BY ts_rank_cd(search_tsv, q, 32) DESC, id
            LIMIT %s""",
        [tsq, *params, limit],
    ).fetchall()
    return [r["id"] for r in rows]


def vector_search(conn, vector, filters: SearchFilters, limit: int) -> list[str]:
    where, params = _where(filters)
    # Filters are applied after the HNSW scan; widen the candidate list so filtered queries still fill up.
    conn.execute(f"SET LOCAL hnsw.ef_search = {max(100, limit * 4)}")
    rows = conn.execute(
        f"""SELECT id FROM jobs WHERE embedding IS NOT NULL AND {where}
            ORDER BY embedding <=> %s, id LIMIT %s""",
        [*params, vector, limit],
    ).fetchall()
    return [r["id"] for r in rows]


def fetch_jobs(conn, ids: list[str]) -> dict[str, Job]:
    if not ids:
        return {}
    rows = conn.execute(f"SELECT {JOB_COLS} FROM jobs WHERE id = ANY(%s)", [ids]).fetchall()
    return {r["id"]: Job(**r) for r in rows}


# ---------------------------------------------------------------- ranking
def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return scores


def preference_boost(job: Job, prefs: Preferences | None) -> float:
    """Multiplicative boost from long-term memory. Kept small so relevance still dominates."""
    if not prefs:
        return 1.0
    boost = 1.0
    loc = job.location.lower()
    if prefs.locations and any(p.lower() in loc for p in prefs.locations):
        boost += 0.15
    if prefs.remote_ok and job.is_remote:
        boost += 0.05
    title = job.title.lower()
    if prefs.roles and any(all(w in title for w in r.lower().split()) for r in prefs.roles):
        boost += 0.10
    return boost


# Checked in order: "Senior Engineering Manager" is a lead role, "Senior Associate" is senior.
_TITLE_LEVELS = [
    (re.compile(r"\b(staff|principal|lead|manager|director|head|vp|architect|distinguished)\b", re.I), 4),
    (re.compile(r"\b(senior|sr)\b", re.I), 3),
    (re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I), 0),
    (re.compile(r"\b(new grad|graduate|junior|jr|associate|entry[- ]level|fresher)\b", re.I), 1),
]
_PROFILE_LEVEL = {"intern": 0, "entry": 1, "mid": 2, "senior": 3, "lead": 4}
EARLY_CAREER_TITLES = r"\m(intern|internship|trainee|apprentice|new grad|graduate|junior|associate|entry level|fresher)\M"


def title_level(title: str) -> int | None:
    for pattern, level in _TITLE_LEVELS:
        if pattern.search(title):
            return level
    return None  # no level word: usually mid-level, treat as neutral


def seniority_fit(job: Job, profile: CandidateProfile | None, *, target_level: int | None = None) -> float:
    """Demote roles far above the candidate's level; a student shouldn't see 'Staff Engineer' first.

    The level comes from the profile, or from the query itself ("internships ...") when there's no profile.
    """
    have = _PROFILE_LEVEL[profile.seniority] if profile else target_level
    if have is None:
        return 1.0
    level = title_level(job.title)
    if level is None:
        return 1.0
    if have == 0 and level == 1:  # final-year students are a fit for new-grad roles
        level = 0
    gap = level - have
    if gap == 0:
        return 1.15
    if gap < 0:
        return 0.9  # over-qualified: mild penalty
    return {1: 0.8, 2: 0.6, 3: 0.45}.get(gap, 0.3)


def apply_preferences(filters: SearchFilters, prefs: Preferences | None) -> SearchFilters:
    if not prefs:
        return filters
    f = filters.model_copy(deep=True)
    if prefs.india_only:
        f.india_only = True
    # prefs.remote_ok is False is applied after retrieval in search(), so it never empties the SQL pool
    f.exclude_companies = sorted(set(f.exclude_companies) | set(prefs.avoid_companies))
    return f


def search(
    conn,
    query: str,
    *,
    method: Method = "hybrid",
    filters: SearchFilters | None = None,
    prefs: Preferences | None = None,
    limit: int = 10,
    llm: LLM | None = None,
    profile: CandidateProfile | None = None,
    query_vector=None,
    early_career: bool = False,
) -> list[ScoredJob]:
    s = get_settings()
    filters = apply_preferences(filters or SearchFilters(), prefs)
    pool = max(s.candidate_pool, limit)
    early_query = is_early_career_query(query)
    early_career = early_career or early_query
    # "internships" stems to 'internship', which doesn't match titles saying "Intern"
    kw_query = f"{query} intern" if early_query else query

    kw = keyword_search(conn, kw_query, filters, pool) if method != "vector" else []
    vec: list[str] = []
    extra: list[list[str]] = []
    if method != "keyword":
        qv = query_vector if query_vector is not None else embed_query(query)
        vec = vector_search(conn, qv, filters, pool)
        if early_career and method in ("hybrid", "hybrid_rerank"):
            # Third candidate source: intern/new-grad roles are a small slice of the index and get crowded
            # out of the general lists, so retrieve them separately and let RRF merge.
            level_filters = filters.model_copy(update={"title_regex": EARLY_CAREER_TITLES})
            extra.append(vector_search(conn, qv, level_filters, pool // 2))

    if method == "keyword":
        scores = {d: 1.0 / (s.rrf_k + r) for r, d in enumerate(kw, 1)}
    elif method == "vector":
        scores = {d: 1.0 / (s.rrf_k + r) for r, d in enumerate(vec, 1)}
    else:
        scores = rrf([kw, vec, *extra], k=s.rrf_k)

    jobs = fetch_jobs(conn, list(scores))
    kw_rank = {d: r for r, d in enumerate(kw, 1)}
    vec_rank = {d: r for r, d in enumerate(vec, 1)}
    results = []
    for doc_id, score in scores.items():
        job = jobs.get(doc_id)
        if job is None:
            continue
        if prefs and prefs.remote_ok is False and job.is_remote:
            continue
        results.append(
            ScoredJob(
                job=job,
                score=score * preference_boost(job, prefs)
                * seniority_fit(job, profile, target_level=0 if early_query else None),
                keyword_rank=kw_rank.get(doc_id),
                vector_rank=vec_rank.get(doc_id),
            )
        )
    results.sort(key=lambda r: (-r.score, r.job.id))

    if method == "hybrid_rerank":
        if llm is None:
            raise ValueError("hybrid_rerank needs an llm")
        head = rerank(llm, query, results[: s.rerank_top_n], profile)
        results = head + results[s.rerank_top_n :]
    return results[:limit]


RERANK_SYSTEM = (
    "You rank job postings for a job seeker. Judge fit on required skills, seniority and role type. "
    "Students and new grads are a poor fit for roles needing 5+ years. Use only the text given; never invent "
    "requirements. Return every job id exactly once. Write each reason to the job seeker in the second person "
    "('you'), naming the concrete skills or requirements that drive the score; use the full 0-10 range."
)


def rerank(llm: LLM, query: str, candidates: list[ScoredJob], profile: CandidateProfile | None) -> list[ScoredJob]:
    if not candidates:
        return []
    listing = "\n\n".join(
        f"[{c.job.id}] {c.job.title} | {c.job.company} | {c.job.location or 'n/a'}\n{c.job.description[:700]}"
        for c in candidates
    )
    who = f"<profile>{profile.model_dump_json()}</profile>\n" if profile else ""
    prompt = f"{who}<query>{query}</query>\n<jobs>\n{listing}\n</jobs>\nScore each job's relevance 0-10."
    result = llm.structured(prompt, RerankResult, system=RERANK_SYSTEM)

    by_id = {c.job.id: c for c in candidates}
    order: list[ScoredJob] = []
    seen: set[str] = set()
    graded = sorted(
        (it for it in result.items if it.job_id in by_id),  # ignore hallucinated ids
        key=lambda it: -it.relevance,
    )
    for it in graded:
        if it.job_id in seen:
            continue
        seen.add(it.job_id)
        c = by_id[it.job_id].model_copy()
        c.reason = it.reason
        c.score = float(it.relevance)
        order.append(c)
    # anything the model skipped keeps its retrieval order, after the graded ones
    order += [c for c in candidates if c.job.id not in seen]
    return order


# ---------------------------------------------------------------- recommendations
def profile_query(profile: CandidateProfile, prefs: Preferences | None) -> str:
    roles = (prefs.roles if prefs and prefs.roles else []) or profile.target_roles
    return " ".join([*roles, *profile.skills[:12], profile.seniority])


def recommend(
    conn,
    profile: CandidateProfile,
    profile_vector,
    *,
    prefs: Preferences | None = None,
    filters: SearchFilters | None = None,
    limit: int = 20,
    llm: LLM | None = None,
) -> list[ScoredJob]:
    """Personalised feed: hybrid retrieval seeded by the profile instead of a typed query."""
    return search(
        conn,
        profile_query(profile, prefs),
        method="hybrid_rerank" if llm else "hybrid",
        filters=filters,
        prefs=prefs,
        limit=limit,
        llm=llm,
        profile=profile,
        query_vector=profile_vector,
        early_career=profile.seniority in ("intern", "entry"),
    )
