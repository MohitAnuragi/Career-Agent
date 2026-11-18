"""Resume-vs-job comparison, shared by the agent tool and the job detail panel."""
from __future__ import annotations

from app.llm.base import LLM
from app.models import CandidateProfile, Job, MatchExplanation

SYSTEM = (
    "You are a blunt, accurate career advisor. matching_skills: every skill from the candidate's profile that the "
    "job asks for or that is directly relevant to the work described. missing_skills: concrete skills or "
    "requirements the job states that the profile lacks. Never invent experience the candidate doesn't have. "
    "Write the summary to the candidate in the second person ('you')."
)


def explain_match(llm: LLM, profile: CandidateProfile, job: Job) -> MatchExplanation:
    prompt = (
        f"<profile>{profile.model_dump_json()}</profile>\n"
        f"<job>{job.title} at {job.company}\n{job.description[:4000]}</job>\n"
        "Compare the candidate to the job's stated requirements."
    )
    return llm.structured(prompt, MatchExplanation, system=SYSTEM)
