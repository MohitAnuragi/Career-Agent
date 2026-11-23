"""Resume (PDF or text) -> validated CandidateProfile via structured output."""
from __future__ import annotations

import io
import time

from pypdf import PdfReader

from app.llm.base import LLM, LLMError
from app.models import CandidateProfile

MAX_RESUME_CHARS = 15000
PDF_FALLBACK_WITHIN_S = 12

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


def _validated(call) -> CandidateProfile:
    try:
        return clean_profile(call())
    except LLMError:
        raise
    except Exception as e:  # schema validation failure
        raise LLMError(f"Model returned an invalid profile: {e}") from e


def extract_profile(llm: LLM, resume_text: str | None, *, pdf: bytes | None = None) -> CandidateProfile:
    """Structured profile from a resume.

    When the model can read PDFs (Gemini), the PDF itself is sent: text extraction scrambles two-column
    layouts and finds nothing in scanned resumes. If that call fails, the extracted text is used instead.
    """
    text = (resume_text or "").strip()[:MAX_RESUME_CHARS]
    if pdf is not None and hasattr(llm, "structured_pdf"):
        started = time.monotonic()
        try:
            return _validated(lambda: llm.structured_pdf(
                pdf, "Extract the candidate profile from the attached resume.", CandidateProfile, system=SYSTEM))
        except LLMError:
            # Retry from text only if the PDF call failed fast (e.g. a rejected file): after a long run of
            # retries a second call would push the request past the host's time limit.
            if len(text) < 100 or time.monotonic() - started > PDF_FALLBACK_WITHIN_S:
                raise
    if len(text) < 100:
        raise ResumeError("Resume text is too short to analyse.")
    prompt = f"Extract the candidate profile from this resume.\n<resume>\n{text}\n</resume>"
    return _validated(lambda: llm.structured(prompt, CandidateProfile, system=SYSTEM))


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def clean_profile(profile: CandidateProfile) -> CandidateProfile:
    """Trim and de-duplicate list fields, keeping their order (skills are ordered by importance)."""
    return profile.model_copy(update={
        "skills": _dedupe(profile.skills),
        "target_roles": _dedupe(profile.target_roles),
        "preferred_locations": _dedupe(profile.preferred_locations),
        "highlights": _dedupe(profile.highlights),
        "name": profile.name.strip(),
        "headline": profile.headline.strip(),
    })
