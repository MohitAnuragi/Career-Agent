"""Pydantic models shared across ingestion, search, the agent and the API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Job(BaseModel):
    id: str
    source: str
    board: str
    company: str
    title: str
    location: str = ""
    is_remote: bool = False
    is_india: bool = False
    employment_type: str = ""
    department: str = ""
    url: str
    description: str = ""
    # From the ATS at ingest; read back from the DB it is the best-known posted time (effective_posted_at).
    posted_at: datetime | None = None
    posted_at_source: str = ""  # ats | first_seen | unknown (DB rows only)
    cities: list[str] = Field(default_factory=list)  # canonical names incl. regions, e.g. ["Gurugram", "Delhi NCR"]
    remote_scope: str = ""  # "" | india | global | unspecified | restricted


Seniority = Literal["intern", "entry", "mid", "senior", "lead"]


class CandidateProfile(BaseModel):
    """Structured output extracted from a resume by the LLM."""

    name: str = Field(description="Candidate's full name, or empty if not found")
    headline: str = Field(description="One-line professional summary, e.g. 'Final-year CS student, Android + backend'")
    seniority: Seniority = Field(description="Career level inferred from experience and graduation date")
    years_experience: float = Field(ge=0, description="Total professional experience in years, internships count")
    skills: list[str] = Field(description="Concrete technical skills, tools and languages, most important first")
    target_roles: list[str] = Field(description="Job titles this person is a good fit for, e.g. 'Backend Engineer'")
    preferred_locations: list[str] = Field(default_factory=list, description="Cities or 'Remote' mentioned in the resume")
    education: str = Field(default="", description="Highest degree, institution and graduation year")
    highlights: list[str] = Field(default_factory=list, description="Up to 5 strongest achievements, each one short sentence")

    def as_search_text(self) -> str:
        """Text used to embed the profile for job matching."""
        parts = [
            self.headline,
            "Roles: " + ", ".join(self.target_roles),
            "Skills: " + ", ".join(self.skills[:25]),
            "Level: " + self.seniority,
        ]
        return ". ".join(p for p in parts if p.strip())


class Preferences(BaseModel):
    """Long-term memory the agent can update. Applied as filters/boosts in ranking."""

    locations: list[str] = Field(default_factory=list, description="Preferred cities")
    remote_ok: bool | None = Field(default=None, description="True to include remote roles, False to exclude them")
    india_only: bool | None = Field(default=None, description="Only show roles located in India")
    roles: list[str] = Field(default_factory=list, description="Preferred role titles")
    avoid_companies: list[str] = Field(default_factory=list, description="Companies to never recommend")
    posted_within_days: int | None = Field(
        default=None, ge=1, le=30, description="Only show jobs posted in the last N days (7 = this week)"
    )
    notes: list[str] = Field(default_factory=list, description="Other durable facts the user shared")


class SearchFilters(BaseModel):
    india_only: bool = False
    remote_only: bool = False
    location: str | None = None  # free text; mapped to canonical cities when possible
    cities: list[str] = Field(default_factory=list)  # canonical names, matched with array overlap
    company: str | None = None
    exclude_job_ids: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    title_regex: str | None = None  # Postgres ~* pattern, used by the early-career candidate source
    posted_within_days: int | None = None  # None = no freshness filter (evals rely on this default)
    include_undated: bool = False  # also show jobs whose posted date is unknown


class ScoredJob(BaseModel):
    job: Job
    score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None
    reason: str | None = None  # filled by the LLM reranker


class RerankItem(BaseModel):
    job_id: str
    relevance: int = Field(ge=0, le=10, description="0 = irrelevant, 10 = perfect fit for this candidate and query")
    reason: str = Field(description="One short sentence citing concrete skills or requirements")


class RerankResult(BaseModel):
    items: list[RerankItem]


class MatchExplanation(BaseModel):
    verdict: Literal["strong", "moderate", "weak"]
    matching_skills: list[str]
    missing_skills: list[str]
    summary: str = Field(description="Two sentences max, honest about gaps")
