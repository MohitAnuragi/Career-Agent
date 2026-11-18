"""Resume (PDF or text) -> validated CandidateProfile via structured output."""
from __future__ import annotations

import io

from pypdf import PdfReader

from app.llm.base import LLM, LLMError
from app.models import CandidateProfile

MAX_RESUME_CHARS = 15000

SYSTEM = (
    "You extract a structured candidate profile from a resume. Only use facts present in the resume. "
    "Skills must be concrete technologies or techniques (e.g. 'Kotlin', 'PostgreSQL', 'REST APIs'), not soft "
    "skills. Infer seniority from graduation year and work history: current students are 'intern'; "
    "0-2 years full-time is 'entry'. Internships count towards years_experience."
)


class ResumeError(ValueError):
    pass


def pdf_to_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:5])
    except Exception as e:  # pypdf raises many types for malformed files
        raise ResumeError(f"Could not read PDF: {e}") from e
    if len(text.strip()) < 100:
        raise ResumeError("No text found in PDF. Is it a scanned image? Paste the resume text instead.")
    return text


def extract_profile(llm: LLM, resume_text: str) -> CandidateProfile:
    text = resume_text.strip()[:MAX_RESUME_CHARS]
    if len(text) < 100:
        raise ResumeError("Resume text is too short to analyse.")
    prompt = f"Extract the candidate profile from this resume.\n<resume>\n{text}\n</resume>"
    try:
        profile = llm.structured(prompt, CandidateProfile, system=SYSTEM)
    except LLMError:
        raise
    except Exception as e:  # schema validation failure
        raise LLMError(f"Model returned an invalid profile: {e}") from e
    # de-duplicate skills while keeping the model's importance order
    seen, skills = set(), []
    for s in profile.skills:
        if s.strip() and s.lower() not in seen:
            seen.add(s.lower())
            skills.append(s.strip())
    profile.skills = skills
    return profile
