"""Tools the career agent can call. Each tool validates its arguments and returns compact JSON."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from app import matching, search, store
from app.config import get_settings
from app.ingest.extract import focus_text
from app.llm.base import LLM, ToolSpec
from app.models import CandidateProfile, Job, Preferences, ScoredJob, SearchFilters


class ToolError(Exception):
    """Expected, user-facing failure; the message is shown to the model so it can recover."""


@dataclass
class ToolContext:
    conn: Any
    user_id: str
    llm: LLM
    profile: CandidateProfile | None
    profile_vector: Any
    prefs: Preferences
    charge: Callable[[], None]  # counts one LLM call against the user's daily cap
    seen_jobs: dict[str, Job] = field(default_factory=dict)  # jobs surfaced this turn, for UI cards


def _card(sj: ScoredJob | Job, ctx: ToolContext) -> dict:
    job = sj.job if isinstance(sj, ScoredJob) else sj
    ctx.seen_jobs[job.id] = job
    out = {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "remote": job.is_remote,
        "posted": job.posted_at.date().isoformat() if job.posted_at else None,
        "url": job.url,
    }
    if isinstance(sj, ScoredJob) and sj.reason:
        out["why"] = sj.reason
    return out


def _need_profile(ctx: ToolContext) -> CandidateProfile:
    if ctx.profile is None:
        raise ToolError("No resume on file. Ask the user to upload their resume first.")
    return ctx.profile


def _need_job(ctx: ToolContext, job_id: str) -> Job:
    job = ctx.seen_jobs.get(job_id) or store.get_job(ctx.conn, job_id)
    if job is None:
        raise ToolError(f"Unknown job id '{job_id}'. Use an id returned by search_jobs or recommend_jobs.")
    return job


def _dismissed(ctx: ToolContext) -> list[str]:
    return store.feedback_ids(ctx.conn, ctx.user_id, "dismiss")


# ---------------------------------------------------------------- argument models
class SearchArgs(BaseModel):
    query: str = Field(min_length=1, description="What to look for, e.g. 'android developer intern' or 'LLM backend'")
    location: str | None = Field(default=None, description="One city or area as the user said it, e.g. 'Bangalore'")
    cities: list[str] = Field(
        default_factory=list,
        description="Cities to include, e.g. ['Pune', 'Mumbai']. 'Delhi NCR' covers Delhi, Gurugram, Noida, "
        "Ghaziabad and Faridabad",
    )
    posted_within_days: int | None = Field(
        default=None, ge=1, le=30,
        description="Only jobs posted in the last N days: 7 = this week, 14 = last two weeks. "
        "Omit to use the user's saved window (default 14)",
    )
    india_only: bool = Field(default=False, description="Only roles located in India")
    remote_only: bool = Field(default=False, description="Only remote roles")
    company: str | None = Field(default=None, description="Company name substring")
    limit: int = Field(default=5, ge=1, le=10)


class RecommendArgs(BaseModel):
    limit: int = Field(default=5, ge=1, le=10)


class JobIdArgs(BaseModel):
    job_id: str = Field(description="Job id exactly as returned by a search tool, e.g. 'lever:cred:aa98...'")


class DraftArgs(JobIdArgs):
    question: str = Field(description="The application question or 'cover note'")


class NoArgs(BaseModel):
    pass


# ---------------------------------------------------------------- implementations
def _window(ctx: ToolContext, requested: int | None = None) -> int:
    return requested or ctx.prefs.posted_within_days or get_settings().fresh_days_default


def search_jobs(ctx: ToolContext, a: SearchArgs) -> dict:
    filters = SearchFilters(
        india_only=a.india_only,
        remote_only=a.remote_only,
        location=a.location,
        cities=a.cities,
        posted_within_days=_window(ctx, a.posted_within_days),
        company=a.company,
        exclude_job_ids=_dismissed(ctx),
    )
    results = search.search(ctx.conn, a.query, method="hybrid", filters=filters, prefs=ctx.prefs, limit=a.limit)
    return {"count": len(results), "jobs": [_card(r, ctx) for r in results]}


def recommend_jobs(ctx: ToolContext, a: RecommendArgs) -> dict:
    profile = _need_profile(ctx)
    for _ in range(search.rerank_calls()):  # one charge per rerank batch
        ctx.charge()
    results = search.recommend(
        ctx.conn,
        profile,
        ctx.profile_vector,
        prefs=ctx.prefs,
        filters=SearchFilters(exclude_job_ids=_dismissed(ctx), posted_within_days=_window(ctx)),
        limit=a.limit,
        llm=ctx.llm,
        user_id=ctx.user_id,
    )
    return {"count": len(results), "jobs": [_card(r, ctx) for r in results]}


def get_job_details(ctx: ToolContext, a: JobIdArgs) -> dict:
    job = _need_job(ctx, a.job_id)
    return {**_card(job, ctx), "department": job.department, "type": job.employment_type,
            "description": focus_text(job.description)[:3000]}


def explain_match(ctx: ToolContext, a: JobIdArgs) -> dict:
    profile, job = _need_profile(ctx), _need_job(ctx, a.job_id)
    ctx.charge()
    result = matching.explain_match(ctx.llm, profile, job)
    return {"job_id": job.id, **result.model_dump()}


def save_job(ctx: ToolContext, a: JobIdArgs) -> dict:
    job = _need_job(ctx, a.job_id)
    store.record_feedback(ctx.conn, ctx.user_id, job.id, "save")
    return {"saved": job.id, "title": job.title, "company": job.company}


def dismiss_job(ctx: ToolContext, a: JobIdArgs) -> dict:
    job = _need_job(ctx, a.job_id)
    store.record_feedback(ctx.conn, ctx.user_id, job.id, "dismiss")
    return {"dismissed": job.id, "note": "It will no longer appear in searches or recommendations."}


def list_saved_jobs(ctx: ToolContext, a: NoArgs) -> dict:
    jobs = store.saved_jobs(ctx.conn, ctx.user_id)
    return {"count": len(jobs), "jobs": [_card(j, ctx) for j in jobs[:20]]}


def update_preferences(ctx: ToolContext, a: Preferences) -> dict:
    changes = a.model_dump(exclude_defaults=True)
    if not changes:
        raise ToolError("No preference fields given.")
    ctx.prefs = store.update_preferences(ctx.conn, ctx.user_id, changes)
    return {"preferences": ctx.prefs.model_dump(exclude_defaults=True)}


def draft_application_answer(ctx: ToolContext, a: DraftArgs) -> dict:
    profile, job = _need_profile(ctx), _need_job(ctx, a.job_id)
    ctx.charge()
    prompt = (
        f"<profile>{profile.model_dump_json()}</profile>\n"
        f"<job>{job.title} at {job.company}\n{focus_text(job.description)[:3000]}</job>\n"
        f"Write the candidate's answer to: {a.question}\n"
        "Under 150 words, first person, specific to this job, using only facts from the profile."
    )
    return {"job_id": job.id, "draft": ctx.llm.text(prompt, system="You help students write honest applications.")}


# ---------------------------------------------------------------- registry
@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[ToolContext, Any], dict]

    def spec(self) -> ToolSpec:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return ToolSpec(self.name, self.description, schema)


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("search_jobs", "Hybrid keyword + semantic search over live job postings.", SearchArgs, search_jobs),
        Tool("recommend_jobs", "Personalised job feed ranked against the user's resume and preferences.",
             RecommendArgs, recommend_jobs),
        Tool("get_job_details", "Full posting text for one job id.", JobIdArgs, get_job_details),
        Tool("explain_match", "Compare the user's resume with one job: matching and missing skills.",
             JobIdArgs, explain_match),
        Tool("save_job", "Bookmark a job for the user.", JobIdArgs, save_job),
        Tool("dismiss_job", "Hide a job the user is not interested in.", JobIdArgs, dismiss_job),
        Tool("list_saved_jobs", "List jobs the user has saved.", NoArgs, list_saved_jobs),
        Tool("update_preferences",
             "Remember durable preferences (locations, remote, India-only, roles, companies to avoid, "
             "how recent postings must be). "
             "Call whenever the user states one.", Preferences, update_preferences),
        Tool("draft_application_answer", "Draft an answer to an application question for one job.",
             DraftArgs, draft_application_answer),
    ]
}
TOOL_SPECS = [t.spec() for t in TOOLS.values()]


def dispatch(ctx: ToolContext, name: str, raw_args: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"Unknown tool '{name}'. Available: {', '.join(TOOLS)}")
    try:
        args = tool.args.model_validate(raw_args or {})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise ToolError(f"Invalid arguments for {name}: {problems}") from e
    return tool.fn(ctx, args)
