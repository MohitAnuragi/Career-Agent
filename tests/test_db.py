"""Integration tests against a real Postgres + pgvector (skipped if the DB is unreachable)."""
import pytest

from app import search, store
from app.ingest.pipeline import embed_pending, sync_board
from app.ingest.sources import normalize_greenhouse
from app.models import Preferences, SearchFilters
from tests.fixtures import GREENHOUSE


def test_incremental_sync_counts_and_reembeds_only_changes(seeded):
    conn = seeded
    jobs = normalize_greenhouse(GREENHOUSE, "groww", "Groww")
    assert sync_board(conn, "greenhouse", "groww", jobs) == (0, 0, 2, 0)  # nothing changed

    jobs[0].description += " Also Kafka."
    assert sync_board(conn, "greenhouse", "groww", jobs[:1]) == (0, 1, 0, 1)  # 1 changed, 1 removed
    conn.commit()
    row = conn.execute("SELECT embedding IS NULL AS pending FROM jobs WHERE id = 'greenhouse:groww:101'").fetchone()
    assert row["pending"]
    gone = conn.execute("SELECT is_active FROM jobs WHERE id = 'greenhouse:groww:102'").fetchone()
    assert gone["is_active"] is False
    assert embed_pending(conn) == 1


def test_keyword_vector_and_hybrid_retrieval(seeded):
    conn = seeded
    f = SearchFilters()
    assert search.keyword_search(conn, "kotlin compose", f, 5)[0] == "lever:cred:aa-1"
    # semantic: no shared keywords with "mobile app developer" in the title, still ranks the Android job first
    from app.embeddings import embed_query
    assert search.vector_search(conn, embed_query("mobile app developer internship"), f, 5)[0] == "lever:cred:aa-1"
    top = search.search(conn, "LLM agents with RAG", method="hybrid", limit=3)
    assert top[0].job.id == "ashby:sarvam:ab-1"
    assert top[0].keyword_rank == 1 and top[0].vector_rank == 1


def test_filters_preferences_and_feedback(seeded):
    conn = seeded
    remote = search.search(conn, "engineer", filters=SearchFilters(remote_only=True), limit=10)
    assert {r.job.id for r in remote} == {"ashby:sarvam:ab-1"}

    india = search.search(conn, "sales account executive", filters=SearchFilters(india_only=True), limit=10)
    assert "greenhouse:groww:102" not in {r.job.id for r in india}  # Indianapolis

    # the Android intern is listed in "Bangalore"; asking for Bengaluru must still find it
    blr = search.search(conn, "android internships", filters=SearchFilters(location="Bengaluru"), limit=5)
    assert blr and blr[0].job.id == "lever:cred:aa-1"

    prefs = Preferences(avoid_companies=["CRED"], remote_ok=False)
    ids = {r.job.id for r in search.search(conn, "engineer developer", prefs=prefs, limit=10)}
    assert "lever:cred:aa-1" not in ids and "ashby:sarvam:ab-1" not in ids

    uid = store.create_user(conn)
    assert store.record_feedback(conn, uid, "lever:cred:aa-1", "dismiss")
    assert not store.record_feedback(conn, uid, "nope", "save")
    assert store.feedback_ids(conn, uid, "dismiss") == ["lever:cred:aa-1"]


def test_preference_memory_merges(conn):
    uid = store.create_user(conn)
    store.update_preferences(conn, uid, {"locations": ["Bengaluru"], "remote_ok": True})
    p = store.update_preferences(conn, uid, {"locations": ["bengaluru", "Gurugram"], "remote_ok": None})
    assert p.locations == ["Bengaluru", "Gurugram"] and p.remote_ok is True


def test_daily_llm_cap(conn):
    uid = store.create_user(conn)
    assert store.charge_llm_call(conn, uid, limit=2) == 1
    assert store.charge_llm_call(conn, uid, limit=2) == 2
    with pytest.raises(store.RateLimited):
        store.charge_llm_call(conn, uid, limit=2)


def test_global_llm_cap_stops_many_anonymous_users(conn):
    # each fresh user is under their own cap, but the shared budget still runs out
    for _ in range(3):
        store.charge_llm_call(conn, store.create_user(conn), limit=10, global_limit=3)
    with pytest.raises(store.RateLimited, match="shared"):
        store.charge_llm_call(conn, store.create_user(conn), limit=10, global_limit=3)


# ---------------------------------------------------------------- freshness, cities, board health
def _ids(results):
    return {r.job.id for r in results}


def test_posting_window_hides_older_jobs(seeded):
    conn = seeded
    q = "data analyst sql dashboards"
    assert "lever:cred:aa-2" in _ids(search.search(conn, q, limit=10))  # no window: evals rely on this
    assert "lever:cred:aa-2" not in _ids(search.search(conn, q, filters=SearchFilters(posted_within_days=14), limit=10))
    assert "lever:cred:aa-2" in _ids(search.search(conn, q, filters=SearchFilters(posted_within_days=30), limit=10))
    week = search.search(conn, "engineer intern", filters=SearchFilters(posted_within_days=7), limit=10)
    assert "lever:cred:aa-1" in _ids(week)


def test_undated_jobs_are_hidden_unless_asked_for(seeded):
    conn = seeded
    row = conn.execute("SELECT posted_at_source, effective_posted_at FROM jobs WHERE id = 'greenhouse:groww:102'").fetchone()
    assert row["posted_at_source"] == "unknown" and row["effective_posted_at"] is None  # first ingest of the board

    q = "sales account executive"
    assert "greenhouse:groww:102" not in _ids(search.search(conn, q, filters=SearchFilters(posted_within_days=14)))
    shown = search.search(conn, q, filters=SearchFilters(posted_within_days=14, include_undated=True))
    assert "greenhouse:groww:102" in _ids(shown)

    # Once the board is known, a newly appearing undated job is dated by when we first saw it...
    jobs = normalize_greenhouse(GREENHOUSE, "groww", "Groww")
    late = jobs[1].model_copy(update={"id": "greenhouse:groww:103", "title": "Account Manager"})
    sync_board(conn, "greenhouse", "groww", [*jobs, late])
    conn.commit()
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, posted_at_source, effective_posted_at FROM jobs WHERE board = 'groww'")}
    assert rows["greenhouse:groww:103"]["posted_at_source"] == "first_seen"
    assert rows["greenhouse:groww:103"]["effective_posted_at"] is not None
    # ...but a re-sync never "refreshes" the job that was already there undated.
    assert rows["greenhouse:groww:102"]["posted_at_source"] == "unknown"


def test_city_filters_use_canonical_names(seeded):
    conn = seeded
    ncr = search.search(conn, "analyst", filters=SearchFilters(cities=["Delhi NCR"]), limit=10)
    assert _ids(ncr) == {"lever:cred:aa-2"}  # listed as "Gurgaon"
    assert _ids(search.search(conn, "analyst", filters=SearchFilters(location="gurugram"), limit=10)) == {"lever:cred:aa-2"}
    assert not search.search(conn, "analyst", filters=SearchFilters(cities=["Pune"]), limit=10)


def test_store_results_filters_counts_and_disables_failing_boards(conn):
    from app.ingest.pipeline import disabled_boards, store_results
    from app.ingest.sources import normalize_lever
    from tests.fixtures import LEVER, LEVER_US_REMOTE, days_ago

    fresh, analyst = normalize_lever(LEVER, "cred", "CRED")
    old = analyst.model_copy(update={"id": "lever:cred:old", "posted_at": days_ago(45)})
    [us_only] = normalize_lever(LEVER_US_REMOTE, "cred", "CRED")
    fetched = {("lever", "cred"): ("CRED", [fresh, analyst, old, us_only])}
    failed = {("lever", "gone"): ("Gone", "HTTPStatusError: 404")}
    stats = store_results(conn, fetched, failed, embed=False)
    assert (stats.fetched, stats.kept, stats.too_old, stats.restricted_remote) == (4, 2, 1, 1)
    assert stats.boards_ok == 1 and "lever:gone" in stats.boards_failed

    for _ in range(5):
        store_results(conn, {}, failed, embed=False)
    assert ("lever", "gone") not in disabled_boards(conn)  # 6 failures
    stats = store_results(conn, {}, failed, embed=False)
    assert stats.boards_disabled == ["lever:gone"] and ("lever", "gone") in disabled_boards(conn)

    store_results(conn, fetched, {}, embed=False)  # a good run resets a board's failure count
    assert conn.execute("SELECT consecutive_failures FROM boards WHERE board = 'cred'").fetchone()["consecutive_failures"] == 0


def test_age_out_and_prune_keep_saved_jobs(seeded):
    from app.ingest.pipeline import age_out, prune

    conn = seeded
    conn.execute("UPDATE jobs SET effective_posted_at = now() - interval '40 days' WHERE id = 'lever:cred:aa-2'")
    assert age_out(conn, 30) == 1
    assert conn.execute("SELECT is_active FROM jobs WHERE id = 'lever:cred:aa-2'").fetchone()["is_active"] is False

    uid = store.create_user(conn)
    conn.execute("UPDATE jobs SET is_active = FALSE, last_seen_at = now() - interval '90 days' "
                 "WHERE id IN ('lever:cred:aa-2', 'greenhouse:groww:101')")
    assert store.record_feedback(conn, uid, "greenhouse:groww:101", "save")
    assert prune(conn, 60) == 1  # aa-2 deleted; the saved job is kept
    left = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
    assert "lever:cred:aa-2" not in left and "greenhouse:groww:101" in left
    assert [j.id for j in store.saved_jobs(conn, uid)] == ["greenhouse:groww:101"]


def test_ingest_stores_skills_and_years_and_reembeds_old_versions(seeded):
    from app.embeddings import EMBED_VERSION
    from app.ingest.pipeline import reprocess

    conn = seeded
    row = conn.execute("SELECT skills, min_years, embedding_version FROM jobs WHERE id = 'greenhouse:groww:101'").fetchone()
    assert {"Python", "PostgreSQL"} <= set(row["skills"]) and row["min_years"] is None
    assert row["embedding_version"] == EMBED_VERSION

    # A job embedded with an older passage format is re-embedded even though its content didn't change.
    conn.execute("UPDATE jobs SET embedding_version = 1, skills = '{}', min_years = 9 WHERE id = 'lever:cred:aa-1'")
    conn.commit()
    assert reprocess(conn) == {"derived": 5, "embedded": 1}  # every stored row; only the outdated one re-embedded
    row = conn.execute("SELECT skills, min_years, embedding_version FROM jobs WHERE id = 'lever:cred:aa-1'").fetchone()
    assert {"Kotlin", "Jetpack Compose"} <= set(row["skills"]) and row["min_years"] is None
    assert row["embedding_version"] == EMBED_VERSION


def test_feed_learns_from_saved_jobs(seeded):
    import numpy as np

    from app.models import CandidateProfile
    from app.search import feedback_centroids, recommend

    conn = seeded
    uid = store.create_user(conn)
    assert feedback_centroids(conn, uid) == (None, None)
    store.record_feedback(conn, uid, "ashby:sarvam:ab-1", "save")
    saved, dismissed = feedback_centroids(conn, uid)
    assert saved is not None and dismissed is None
    stored = conn.execute("SELECT embedding FROM jobs WHERE id = 'ashby:sarvam:ab-1'").fetchone()["embedding"]
    assert np.allclose(saved, search.as_array(stored), atol=1e-5)  # the mean of one job is that job

    profile = CandidateProfile(name="", headline="Android developer", seniority="intern", years_experience=0.5,
                               skills=["Kotlin"], target_roles=["Android Developer"])
    from app.embeddings import embed_query
    vec = embed_query(profile.as_search_text())
    plain = recommend(conn, profile, vec, limit=5)
    learned = recommend(conn, profile, vec, limit=5, user_id=uid)
    rank = lambda rs: [r.job.id for r in rs].index("ashby:sarvam:ab-1")  # noqa: E731
    assert rank(learned) <= rank(plain)
    assert plain[0].job.id == "lever:cred:aa-1" and "Kotlin" in plain[0].matched_skills
