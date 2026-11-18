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
