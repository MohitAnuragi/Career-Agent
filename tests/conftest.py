import os
import re

# Must run before any app module reads settings.
os.environ["LLM_PROVIDER"] = "fake"
_base = os.environ.get("DATABASE_URL", "postgresql://career:career@localhost:5432/career_agent")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or re.sub(r"/[^/?]+(\?|$)", r"/career_agent_test\1", _base)
os.environ["DAILY_LLM_LIMIT"] = "1000"

import pytest  # noqa: E402

TABLES = "llm_usage_global, llm_usage, messages, feedback, users, ingest_runs, boards, jobs"


@pytest.fixture(scope="session")
def db_url():
    import psycopg

    url = os.environ["DATABASE_URL"]
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except Exception as e:  # pragma: no cover - depends on environment
        pytest.skip(f"Postgres not reachable at {url}: {e}")
    return url


@pytest.fixture
def conn(db_url):
    from app.db import connection

    with connection(db_url) as c:
        c.execute(f"TRUNCATE {TABLES} CASCADE")
        c.commit()
        yield c
        c.rollback()


@pytest.fixture
def seeded(conn):
    """5 active jobs from fixture payloads (one per interesting case), embedded with the real model."""
    from app.ingest.pipeline import embed_pending, sync_board
    from app.ingest.sources import normalize_ashby, normalize_greenhouse, normalize_lever
    from tests.fixtures import ASHBY, GREENHOUSE, LEVER

    sync_board(conn, "greenhouse", "groww", normalize_greenhouse(GREENHOUSE, "groww", "Groww"))
    sync_board(conn, "lever", "cred", normalize_lever(LEVER, "cred", "CRED"))
    sync_board(conn, "ashby", "sarvam", normalize_ashby(ASHBY, "sarvam", "Sarvam AI"))
    conn.commit()
    assert embed_pending(conn) == 5
    return conn
