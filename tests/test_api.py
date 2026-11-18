"""End-to-end through HTTP with the offline LLM (no API key needed)."""
import pytest
from fastapi.testclient import TestClient

RESUME = """Mohit Anuragi
B.Tech Computer Science student, IIIT Kota (2027)
Skills: Kotlin, Jetpack Compose, Android, Python, FastAPI, PostgreSQL, REST APIs, Git
Experience: Android Development Intern - shipped features for apps with 10K+ downloads.
Built a neural network from scratch in NumPy. Based in Bengaluru."""


@pytest.fixture
def client(seeded):
    from app.api import app

    with TestClient(app) as c:
        yield c


def test_full_flow(client):
    assert client.get("/api/health").json()["active_jobs"] == 4
    assert client.get("/api/profile").status_code == 401

    uid = client.post("/api/users").json()["user_id"]
    h = {"X-User-Id": uid}

    assert client.get("/api/recommendations", headers=h).status_code == 409  # no resume yet
    prof = client.post("/api/profile", headers=h, data={"text": RESUME}).json()["profile"]
    assert "Kotlin" in prof["skills"] and prof["seniority"] == "intern"

    recs = client.get("/api/recommendations", headers=h).json()["jobs"]
    assert "lever:cred:aa-1" in [j["id"] for j in recs[:2]]  # Android intern near the top for an Android resume

    s = client.get("/api/search", params={"q": "LLM agents", "india_only": True}).json()["jobs"]
    assert s[0]["id"] == "ashby:sarvam:ab-1"

    assert client.post("/api/feedback", headers=h, json={"job_id": recs[0]["id"], "action": "save"}).json()["ok"]
    assert [j["id"] for j in client.get("/api/saved", headers=h).json()["jobs"]] == [recs[0]["id"]]
    client.post("/api/feedback", headers=h, json={"job_id": recs[0]["id"], "action": "dismiss"})
    recs2 = client.get("/api/recommendations", headers=h).json()["jobs"]
    assert recs[0]["id"] not in {j["id"] for j in recs2}

    chat = client.post("/api/chat", headers=h, json={"message": "android developer"}).json()
    assert chat["trace"][0]["tool"] == "search_jobs" and chat["trace"][0]["ok"]
    assert "Android Developer Intern" in chat["reply"] or chat["jobs"]
    assert len(client.get("/api/chat", headers=h).json()["messages"]) == 2


def test_admin_ingest_requires_token(client):
    assert client.post("/admin/ingest").status_code == 403
