"""HTTP API + static web UI.  Run: uvicorn app.api:app --reload"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import search, store
from app.agent.loop import run_agent
from app.agent.tools import ToolContext
from app.config import get_settings
from app.db import close_pools, connection, get_pool
from app.embeddings import embed_query
from app.llm import LLMError, get_llm
from app.models import Preferences, ScoredJob, SearchFilters
from app.profile import ResumeError, extract_profile, pdf_to_text

log = logging.getLogger("api")
STATIC = Path(__file__).with_name("static")
MAX_UPLOAD = 2 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_pool()  # creates the schema on first boot
    yield
    close_pools()


app = FastAPI(title="Career Agent", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- errors
@app.exception_handler(store.RateLimited)
async def _rate_limited(_: Request, e: store.RateLimited):
    return JSONResponse(status_code=429, content={"detail": str(e)})


@app.exception_handler(LLMError)
async def _llm_error(_: Request, e: LLMError):
    log.warning("LLM error: %s", e)
    return JSONResponse(status_code=502, content={"detail": f"AI provider error: {e}"})


@app.exception_handler(ResumeError)
async def _resume_error(_: Request, e: ResumeError):
    return JSONResponse(status_code=400, content={"detail": str(e)})


# ---------------------------------------------------------------- dependencies
def get_conn():
    with connection() as conn:
        yield conn


Conn = Annotated[object, Depends(get_conn)]


def get_user(conn: Conn, x_user_id: Annotated[str | None, Header()] = None) -> str:
    if not x_user_id or not store.user_exists(conn, x_user_id):
        raise HTTPException(401, "Unknown user. Call POST /api/users first and send X-User-Id.")
    return x_user_id


User = Annotated[str, Depends(get_user)]


def charger(conn, user_id: str):
    limit = get_settings().daily_llm_limit
    return lambda: store.charge_llm_call(conn, user_id, limit)


def job_out(r: ScoredJob) -> dict:
    j = r.job
    return {
        "id": j.id, "title": j.title, "company": j.company, "location": j.location, "remote": j.is_remote,
        "india": j.is_india, "url": j.url, "department": j.department, "source": j.source,
        "posted_at": j.posted_at.isoformat() if j.posted_at else None,
        "snippet": j.description[:280], "score": round(r.score, 5), "reason": r.reason,
        "keyword_rank": r.keyword_rank, "vector_rank": r.vector_rank,
    }


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health(conn: Conn):
    row = conn.execute("SELECT count(*) AS n FROM jobs WHERE is_active").fetchone()
    return {"ok": True, "active_jobs": row["n"], "llm": get_settings().llm_provider}


@app.get("/api/stats")
def stats(conn: Conn):
    totals = conn.execute(
        """SELECT count(*) FILTER (WHERE is_active) AS active,
                  count(*) FILTER (WHERE is_active AND is_india) AS india,
                  count(*) FILTER (WHERE is_active AND is_remote) AS remote,
                  count(*) FILTER (WHERE is_active AND embedding IS NULL) AS unembedded,
                  count(DISTINCT company) FILTER (WHERE is_active) AS companies
           FROM jobs"""
    ).fetchone()
    last = conn.execute(
        "SELECT started_at, finished_at, stats FROM ingest_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"jobs": totals, "last_ingest": last}


@app.post("/api/users")
def new_user(conn: Conn):
    return {"user_id": store.create_user(conn)}


@app.post("/api/profile")
def upload_profile(  # sync on purpose: DB + LLM calls run in FastAPI's threadpool, not the event loop
    conn: Conn,
    user: User,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
):
    if file is not None:
        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Resume must be under 2 MB.")
        resume = pdf_to_text(data)
    elif text:
        resume = text
    else:
        raise HTTPException(400, "Send a PDF file or resume text.")
    store.charge_llm_call(conn, user, get_settings().daily_llm_limit)
    profile = extract_profile(get_llm(), resume)  # the raw resume is never stored
    store.save_profile(conn, user, profile, embed_query(profile.as_search_text()))
    return {"profile": profile, "preferences": store.get_preferences(conn, user)}


@app.get("/api/profile")
def read_profile(conn: Conn, user: User):
    profile, _ = store.get_profile(conn, user)
    return {"profile": profile, "preferences": store.get_preferences(conn, user)}


@app.put("/api/preferences")
def put_preferences(conn: Conn, user: User, prefs: Preferences):
    return {"preferences": store.set_preferences(conn, user, prefs)}


@app.get("/api/recommendations")
def recommendations(
    conn: Conn,
    user: User,
    limit: int = Query(20, ge=1, le=50),
    rerank: bool = True,
):
    profile, vector = store.get_profile(conn, user)
    if profile is None:
        raise HTTPException(409, "Upload a resume first.")
    llm = None
    if rerank:
        store.charge_llm_call(conn, user, get_settings().daily_llm_limit)
        llm = get_llm()
    results = search.recommend(
        conn,
        profile,
        vector,
        prefs=store.get_preferences(conn, user),
        filters=SearchFilters(exclude_job_ids=store.feedback_ids(conn, user, "dismiss")),
        limit=limit,
        llm=llm,
    )
    return {"jobs": [job_out(r) for r in results]}


@app.get("/api/search")
def search_jobs(
    conn: Conn,
    q: str = Query(..., min_length=1, max_length=300),
    method: search.Method = "hybrid",
    india_only: bool = False,
    remote_only: bool = False,
    location: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    x_user_id: Annotated[str | None, Header()] = None,
):
    prefs, dismissed, profile, llm = None, [], None, None
    known_user = bool(x_user_id) and store.user_exists(conn, x_user_id)
    if known_user:
        prefs = store.get_preferences(conn, x_user_id)
        dismissed = store.feedback_ids(conn, x_user_id, "dismiss")
        profile, _ = store.get_profile(conn, x_user_id)
    if method == "hybrid_rerank":
        if not known_user:
            raise HTTPException(401, "Reranking needs a user (X-User-Id).")
        store.charge_llm_call(conn, x_user_id, get_settings().daily_llm_limit)
        llm = get_llm()
    filters = SearchFilters(india_only=india_only, remote_only=remote_only, location=location,
                            exclude_job_ids=dismissed)
    results = search.search(conn, q, method=method, filters=filters, prefs=prefs, limit=limit, llm=llm,
                            profile=profile)
    return {"jobs": [job_out(r) for r in results]}


class FeedbackIn(BaseModel):
    job_id: str
    action: Literal["save", "dismiss", "clear"]


@app.post("/api/feedback")
def feedback(conn: Conn, user: User, body: FeedbackIn):
    if body.action == "clear":
        store.clear_feedback(conn, user, body.job_id)
    elif not store.record_feedback(conn, user, body.job_id, body.action):
        raise HTTPException(404, "Unknown job.")
    return {"ok": True}


@app.get("/api/saved")
def saved(conn: Conn, user: User):
    return {"jobs": [job_out(ScoredJob(job=j, score=0)) for j in store.saved_jobs(conn, user)]}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/chat")
def chat(conn: Conn, user: User, body: ChatIn):
    profile, vector = store.get_profile(conn, user)
    ctx = ToolContext(
        conn=conn, user_id=user, llm=get_llm(), profile=profile, profile_vector=vector,
        prefs=store.get_preferences(conn, user), charge=charger(conn, user),
    )
    result = run_agent(ctx.llm, ctx, store.recent_messages(conn, user), body.message)
    store.append_messages(conn, user, [("user", body.message), ("assistant", result.reply)])
    return {**asdict(result), "preferences": ctx.prefs}


@app.get("/api/chat")
def chat_history(conn: Conn, user: User):
    return {"messages": store.recent_messages(conn, user, limit=50)}


@app.delete("/api/chat")
def clear_chat(conn: Conn, user: User):
    store.clear_messages(conn, user)
    return {"ok": True}


_ingest_lock = threading.Lock()


@app.post("/admin/ingest")
def trigger_ingest(authorization: Annotated[str | None, Header()] = None):
    token = get_settings().admin_token
    if not token or authorization != f"Bearer {token}":
        raise HTTPException(403, "Forbidden")
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(409, "An ingest run is already in progress.")

    def work():
        from app.ingest.pipeline import run_ingest

        try:
            run_ingest()
        finally:
            _ingest_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


# ---------------------------------------------------------------- web UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
