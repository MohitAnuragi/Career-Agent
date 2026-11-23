"""Retrieval and ranking.

    candidates:  Postgres full-text (OR query, ts_rank_cd)   +   pgvector cosine (HNSW)
    fusion:      Reciprocal Rank Fusion  score = sum 1 / (k + rank)
    personalise: preference boosts, dismissed jobs / avoided companies filtered out
    rerank:      optional LLM pass over the top N with structured output + per-job reason
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

import numpy as np

from app.config import get_settings
from app.embeddings import embed_query
from app.ingest.cities import canonical_cities, canonical_list, strip_cities
from app.ingest.extract import canonical_skills, find_skills, focus_text
from app.llm.base import LLM, LLMError
from app.models import CandidateProfile, Job, Preferences, RerankItem, RerankResult, ScoredJob, SearchFilters

Method = Literal["keyword", "vector", "hybrid", "hybrid_rerank"]
METHODS: tuple[Method, ...] = ("keyword", "vector", "hybrid", "hybrid_rerank")

# posted_at is read back as the best-known posted time; every column is a plain name (store.py prefixes them).
JOB_COLS = (
    "id, source, board, company, title, location, is_remote, is_india, employment_type, "
    "department, url, description, effective_posted_at AS posted_at, posted_at_source, cities, remote_scope, "
    "skills, min_years"
)
_TOKEN = re.compile(r"[a-z0-9]+")
Sort = Literal["relevance", "newest"]

# A typed query asking for early-career roles ("internships in Pune", "new grad roles").
_EARLY_QUERY = re.compile(r"\b(interns?|internships?|new grads?|graduates?|freshers?|entry[- ]level|trainees?)\b", re.I)


def is_early_career_query(query: str) -> bool:
    return bool(_EARLY_QUERY.search(query))


def filter_cities(f: SearchFilters) -> tuple[list[str], str | None]:
    """Canonical cities to match, plus a leftover free-text location that isn't a known city."""
    cities = canonical_list(f.cities)
    loose = None
    if f.location:
        mapped = canonical_cities(f.location)
        if mapped:
            cities = canonical_list([*cities, *mapped])
        else:
            loose = f.location.strip()
    return cities, loose


# ---------------------------------------------------------------- SQL helpers
def _where(f: SearchFilters) -> tuple[str, list]:
    clauses, params = ["is_active"], []
    if f.india_only:
        clauses.append("is_india")
    if f.remote_only:
        clauses.append("is_remote")
    cities, loose = filter_cities(f)
    if cities:
        clauses.append("cities && %s::text[]")
        params.append(cities)
    if loose:
        clauses.append("location ILIKE %s")
        params.append(f"%{loose}%")
    if f.posted_within_days:
        clauses.append(
            "(effective_posted_at >= now() - make_interval(days => %s::int) OR (%s AND posted_at_source = 'unknown'))"
        )
        params += [f.posted_within_days, f.include_undated]
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
def rrf(rankings: list[list[str]], k: int = 60, weights: list[float] | None = None) -> dict[str, float]:
    """Reciprocal Rank Fusion; `weights` scales each list's contribution (default: all 1.0)."""
    scores: dict[str, float] = {}
    for i, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[i]
        if w == 0:
            continue
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + w / (k + rank)
    return scores


def location_match(job: Job, locations: list[str]) -> bool:
    if set(canonical_list(locations)) & set(job.cities):
        return True
    loc = job.location.lower()
    return any(p.lower() in loc for p in locations)


FRESH_BOOST_DAYS = 3


def freshness_boost(job: Job, now: datetime | None = None) -> float:
    """Tiny nudge for postings from the last few days; small enough that relevance still dominates."""
    if job.posted_at is None:
        return 1.0
    age = (now or datetime.now(timezone.utc)) - job.posted_at
    return 1.05 if age.days < FRESH_BOOST_DAYS else 1.0


def dedupe_key(job: Job) -> tuple:
    """Same company + same title (ignoring a city suffix) + same cities = one card."""
    title = " ".join(_TOKEN.findall(strip_cities(job.title).lower()))
    place = tuple(sorted(job.cities)) or (job.location.lower(),)
    return job.company.lower(), title, place


def dedupe(results: list[ScoredJob]) -> list[ScoredJob]:
    seen: set[tuple] = set()
    out = []
    for r in results:
        key = dedupe_key(r.job)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def preference_boost(job: Job, prefs: Preferences | None) -> float:
    """Multiplicative boost from long-term memory. Kept small so relevance still dominates."""
    if not prefs:
        return 1.0
    boost = 1.0
    if prefs.locations and location_match(job, prefs.locations):
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


def experience_fit(job: Job, profile: CandidateProfile | None, *, target_years: float | None = None) -> float:
    """Demote postings that ask for clearly more experience than the candidate has.

    Title words miss most of these ("Software Engineer" asking for 5+ years). A year of slack is allowed,
    since postings overstate requirements. Without a profile, an early-career query counts as 0 years.
    """
    have = profile.years_experience if profile else target_years
    if have is None or job.min_years is None:
        return 1.0
    gap = job.min_years - have
    if gap <= 1:
        return 1.0
    if gap <= 2:
        return 0.85
    if gap <= 4:
        return 0.65
    return 0.45


FIT_FLOOR = 0.2  # seniority x experience penalties stack; keep a strong match from vanishing entirely


def skill_fit(job: Job, wanted: set[str]) -> tuple[float, list[str]]:
    """Up to +20% for postings that ask for the candidate's (or the query's) skills, plus the shared skills.

    Only a boost, never a penalty: the tags come from a fixed vocabulary, so a posting with no tags or other
    skills isn't necessarily a poor match.
    """
    if not wanted or not job.skills:
        return 1.0, []
    shared = [sk for sk in job.skills if sk in wanted]
    return 1.0 + 0.2 * min(1.0, len(shared) / min(len(job.skills), 6)), shared


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
    sort: Sort = "relevance",
) -> list[ScoredJob]:
    s = get_settings()
    now = datetime.now(timezone.utc)
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
        # The keyword list is much weaker than the vector list (see evals/README.md), so it counts for less.
        scores = rrf([kw, vec, *extra], k=s.rrf_k,
                     weights=[s.rrf_keyword_weight, 1.0, *([s.rrf_early_weight] * len(extra))])

    jobs = fetch_jobs(conn, list(scores))
    kw_rank = {d: r for r, d in enumerate(kw, 1)}
    vec_rank = {d: r for r, d in enumerate(vec, 1)}
    wanted = set(canonical_skills(profile.skills)) if profile else set(find_skills(query))
    results = []
    for doc_id, score in scores.items():
        job = jobs.get(doc_id)
        if job is None:
            continue
        if prefs and prefs.remote_ok is False and job.is_remote:
            continue
        fit = max(FIT_FLOOR, seniority_fit(job, profile, target_level=0 if early_query else None)
                  * experience_fit(job, profile, target_years=0 if early_query else None))
        skills_boost, shared = skill_fit(job, wanted)
        results.append(
            ScoredJob(
                job=job,
                score=score * preference_boost(job, prefs) * freshness_boost(job, now) * fit * skills_boost,
                keyword_rank=kw_rank.get(doc_id),
                vector_rank=vec_rank.get(doc_id),
                matched_skills=shared,
            )
        )
    results.sort(key=lambda r: (-r.score, r.job.id))
    results = dedupe(results)

    if sort == "newest":  # ordering by date makes an LLM relevance pass pointless, so it's skipped
        results.sort(key=lambda r: (r.job.posted_at is None, -(r.job.posted_at or now).timestamp(), -r.score))
        return results[:limit]
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


RERANK_TEXT_CHARS = 800


def rerank_calls(n: int | None = None) -> int:
    """LLM calls one rerank of the top `n` (default: rerank_top_n) makes; the daily caps charge this many."""
    s = get_settings()
    n = s.rerank_top_n if n is None else n
    return max(1, math.ceil(n / s.rerank_batch_size))


def _rerank_listing(c: ScoredJob) -> str:
    j = c.job
    need = f"Requires {j.min_years}+ years of experience\n" if j.min_years else ""
    # Role and requirement sections first: a posting's first 800 raw characters are often the company pitch.
    return f"[{j.id}] {j.title} | {j.company} | {j.location or 'n/a'}\n{need}{focus_text(j.description)[:RERANK_TEXT_CHARS]}"


def _grade(llm: LLM, query: str, batch: list[ScoredJob], profile: CandidateProfile | None) -> list[RerankItem]:
    listing = "\n\n".join(_rerank_listing(c) for c in batch)
    who = f"<profile>{profile.model_dump_json()}</profile>\n" if profile else ""
    prompt = f"{who}<query>{query}</query>\n<jobs>\n{listing}\n</jobs>\nScore each job's relevance 0-10."
    return llm.structured(prompt, RerankResult, system=RERANK_SYSTEM).items


def rerank(llm: LLM, query: str, candidates: list[ScoredJob], profile: CandidateProfile | None) -> list[ScoredJob]:
    """LLM grades 0-10 with a reason. More than `rerank_batch_size` candidates are graded in parallel batches;
    batches are interleaved by rank (1, 3, 5... / 2, 4, 6...) so each one spans the whole list and the grades
    stay comparable. Ties keep retrieval order; a failed batch keeps its jobs ungraded unless every batch fails."""
    if not candidates:
        return []
    n_batches = rerank_calls(len(candidates))
    batches = [candidates[i::n_batches] for i in range(n_batches)]
    if n_batches == 1:
        outcomes: list = [_grade(llm, query, batches[0], profile)]
    else:
        with ThreadPoolExecutor(n_batches) as pool:
            futures = [pool.submit(_grade, llm, query, b, profile) for b in batches]
            outcomes = []
            for f in futures:
                try:
                    outcomes.append(f.result())
                except LLMError as e:
                    outcomes.append(e)
        if all(isinstance(o, LLMError) for o in outcomes):
            raise outcomes[0]
    items = [it for o in outcomes if not isinstance(o, LLMError) for it in o]

    by_id = {c.job.id: c for c in candidates}
    position = {c.job.id: i for i, c in enumerate(candidates)}
    order: list[ScoredJob] = []
    seen: set[str] = set()
    graded = sorted(
        (it for it in items if it.job_id in by_id),  # ignore hallucinated ids
        key=lambda it: (-it.relevance, position[it.job_id]),
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
    """Keyword-side query for the feed: target roles and the top skills.

    The vector side uses the full profile embedding, so this stays short: a 40-term OR query over every skill
    plus the word "mid" or "intern" matched almost anything. Seniority is handled by seniority_fit.
    """
    roles = (prefs.roles if prefs and prefs.roles else []) or profile.target_roles
    return " ".join([*roles, *profile.skills[:6]])


# Rocchio weights: how far saved jobs pull the profile vector, and dismissed jobs push it away. Dismissals are
# often about location or company rather than the work, so they count for less.
SAVED_WEIGHT = 0.5
DISMISSED_WEIGHT = 0.15
FEEDBACK_WINDOW = 30  # most recent saves / dismissals used


def feedback_centroids(conn, user_id: str):
    """Mean embedding of the user's recent saved and dismissed jobs (None when there are none)."""
    out = {}
    for action in ("save", "dismiss"):
        row = conn.execute(
            """SELECT avg(embedding) AS v FROM (
                   SELECT j.embedding FROM feedback f JOIN jobs j ON j.id = f.job_id
                   WHERE f.user_id = %s AND f.action = %s AND j.embedding IS NOT NULL
                   ORDER BY f.created_at DESC LIMIT %s) t""",
            [user_id, action, FEEDBACK_WINDOW],
        ).fetchone()
        out[action] = None if row["v"] is None else as_array(row["v"])
    return out["save"], out["dismiss"]


def as_array(v) -> np.ndarray:
    """pgvector returns its own Vector type for some queries (e.g. avg()); numpy for the maths."""
    return np.asarray(v.to_numpy() if hasattr(v, "to_numpy") else v, dtype=np.float32)


def personal_vector(profile_vector, saved=None, dismissed=None):
    """Profile embedding moved toward saved jobs and away from dismissed ones, re-normalised."""
    base = as_array(profile_vector)
    if saved is None and dismissed is None:
        return profile_vector
    v = base.copy()
    if saved is not None:
        v = v + SAVED_WEIGHT * as_array(saved)
    if dismissed is not None:
        v = v - DISMISSED_WEIGHT * as_array(dismissed)
    norm = float(np.linalg.norm(v))
    return v / norm if norm else base


def recommend(
    conn,
    profile: CandidateProfile,
    profile_vector,
    *,
    prefs: Preferences | None = None,
    filters: SearchFilters | None = None,
    limit: int = 20,
    llm: LLM | None = None,
    user_id: str | None = None,
) -> list[ScoredJob]:
    """Personalised feed: hybrid retrieval seeded by the profile instead of a typed query.

    With a `user_id`, the profile vector learns from that user's saves and dismissals.
    """
    if user_id is not None:
        profile_vector = personal_vector(profile_vector, *feedback_centroids(conn, user_id))
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
