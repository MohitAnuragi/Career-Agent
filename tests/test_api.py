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
    assert client.get("/api/health").json()["active_jobs"] == 5
    assert client.get("/api/profile").status_code == 401

    uid = client.post("/api/users").json()["user_id"]
    h = {"X-User-Id": uid}

    assert client.get("/api/recommendations", headers=h).status_code == 409  # no resume yet
    prof = client.post("/api/profile", headers=h, data={"text": RESUME}).json()["profile"]
    assert "Kotlin" in prof["skills"] and prof["seniority"] == "intern"

    first = client.get("/api/recommendations", headers=h).json()
    recs = first["jobs"]
    assert "lever:cred:aa-1" in [j["id"] for j in recs[:2]]  # Android intern near the top for an Android resume
    again = client.get("/api/recommendations", headers=h).json()
    assert not first["cached"] and again["cached"] and again["jobs"] == recs  # reload costs no AI call

    assert "lever:cred:aa-2" not in {j["id"] for j in recs}  # 20 days old: outside the default 14-day window

    s = client.get("/api/search", params={"q": "LLM agents", "india_only": True}).json()["jobs"]
    assert s[0]["id"] == "ashby:sarvam:ab-1"

    assert client.post("/api/feedback", headers=h, json={"job_id": recs[0]["id"], "action": "save"}).json()["ok"]
    assert [j["id"] for j in client.get("/api/saved", headers=h).json()["jobs"]] == [recs[0]["id"]]
    client.post("/api/feedback", headers=h, json={"job_id": recs[0]["id"], "action": "dismiss"})
    recs2 = client.get("/api/recommendations", headers=h).json()["jobs"]
    assert recs[0]["id"] not in {j["id"] for j in recs2}

    detail = client.get("/api/jobs/ashby:sarvam:ab-1").json()
    assert "tool calling" in detail["description"]
    assert client.get("/api/jobs/nope:x:1").status_code == 404
    match = client.post("/api/jobs/lever:cred:aa-1/match", headers=h).json()
    assert match["verdict"] in {"strong", "moderate", "weak"} and "Kotlin" in match["matching_skills"]

    chat = client.post("/api/chat", headers=h, json={"message": "android developer"}).json()
    assert chat["trace"][0]["tool"] == "search_jobs" and chat["trace"][0]["ok"]
    assert "Android Developer Intern" in chat["reply"] or chat["jobs"]
    assert len(client.get("/api/chat", headers=h).json()["messages"]) == 2


def test_admin_ingest_requires_token(client):
    assert client.post("/admin/ingest").status_code == 403


def test_search_window_cities_and_sort(client):
    def ids(**params):
        return [j["id"] for j in client.get("/api/search", params={"q": "analyst engineer", **params}).json()["jobs"]]

    assert "lever:cred:aa-2" not in ids()  # default 14 days
    assert ids(cities="Delhi NCR", posted_within_days=30) == ["lever:cred:aa-2"]
    assert ids(cities=["Pune", "Delhi NCR"], posted_within_days=30) == ["lever:cred:aa-2"]
    newest = client.get("/api/search", params={"q": "engineer", "sort": "newest"}).json()["jobs"]
    dates = [j["posted_at"] for j in newest]
    assert dates == sorted(dates, reverse=True)
    assert client.get("/api/search", params={"q": "x", "posted_within_days": 90}).status_code == 422

    stats = client.get("/api/stats").json()
    assert stats["fresh"]["this_week"] == 3 and stats["fresh"]["undated"] == 1
    cities = {c["city"]: c for c in stats["cities"]}
    assert cities["Delhi NCR"]["jobs"] == 1 and cities["Delhi NCR"]["fresh"] == 0
    assert "Delhi NCR" in stats["featured_cities"] and stats["boards"]["total"] == 3
