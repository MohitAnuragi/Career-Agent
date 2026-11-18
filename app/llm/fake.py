"""LLM stand-ins.

OfflineLLM: deterministic heuristics so the whole app runs without an API key (LLM_PROVIDER=fake).
ScriptedLLM: returns pre-programmed turns; used by unit tests to drive the agent loop.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.llm.base import LLMError, T, ToolCall, ToolSpec, Turn
from app.models import CandidateProfile, MatchExplanation, RerankItem, RerankResult

SKILL_VOCAB = [
    "Python", "Java", "Kotlin", "JavaScript", "TypeScript", "C++", "C", "Go", "Rust", "SQL", "Swift",
    "React", "Next.js", "Node.js", "React Native", "Android", "Jetpack Compose", "Flutter", "iOS",
    "FastAPI", "Django", "Flask", "Spring", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Firebase",
    "Supabase", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Git", "REST", "GraphQL",
    "Machine Learning", "Deep Learning", "NumPy", "Pandas", "scikit-learn", "PyTorch", "TensorFlow",
    "LLM", "RAG", "Embeddings", "NLP", "Data Structures", "Algorithms", "System Design",
]
ROLE_HINTS = [
    ({"Android", "Kotlin", "Jetpack Compose"}, "Android Developer"),
    ({"Python", "FastAPI", "Django", "Flask", "PostgreSQL", "SQL"}, "Backend Engineer"),
    ({"React", "Next.js", "TypeScript", "JavaScript"}, "Frontend Engineer"),
    ({"Machine Learning", "PyTorch", "LLM", "NumPy", "Pandas"}, "Machine Learning Engineer"),
]


def _between(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1) if m else text


def find_skills(text: str) -> list[str]:
    found = []
    for s in SKILL_VOCAB:
        if re.search(rf"(?<![\w+#.]){re.escape(s)}(?![\w+#])", text, re.IGNORECASE):
            found.append(s)
    return found


class OfflineLLM:
    name = "offline"

    def structured(self, prompt: str, schema: type[T], *, system: str | None = None) -> T:
        if schema is CandidateProfile:
            return self._profile(_between(prompt, "resume"))  # type: ignore[return-value]
        if schema is RerankResult:
            ids = re.findall(r"^\[(\S+)\]", _between(prompt, "jobs"), re.MULTILINE)
            items = [
                RerankItem(job_id=i, relevance=max(1, 9 - n // 2), reason="Offline mode: kept retrieval order.")
                for n, i in enumerate(ids)
            ]
            return RerankResult(items=items)  # type: ignore[return-value]
        if schema is MatchExplanation:
            have = set(find_skills(_between(prompt, "profile")))
            need = set(find_skills(_between(prompt, "job")))
            match, missing = sorted(have & need), sorted(need - have)
            verdict = "strong" if len(match) >= 4 else "moderate" if match else "weak"
            return MatchExplanation(  # type: ignore[return-value]
                verdict=verdict,
                matching_skills=match,
                missing_skills=missing,
                summary=f"Keyword overlap: {len(match)} shared skills, {len(missing)} missing (offline estimate).",
            )
        raise LLMError(f"OfflineLLM has no heuristic for {schema.__name__}")

    def _profile(self, resume: str) -> CandidateProfile:
        lines = [l.strip() for l in resume.splitlines() if l.strip()]
        skills = find_skills(resume)
        roles = [role for keys, role in ROLE_HINTS if keys & set(skills)] or ["Software Engineer"]
        student = re.search(r"\b(b\.?tech|student|undergraduate|intern)\b", resume, re.IGNORECASE)
        return CandidateProfile(
            name=lines[0][:60] if lines else "",
            headline=f"{'Student' if student else 'Engineer'} with {', '.join(skills[:4]) or 'software'} experience",
            seniority="intern" if student else "entry",
            years_experience=0.5 if student else 1.0,
            skills=skills,
            target_roles=roles,
            preferred_locations=[c for c in ("Bengaluru", "Gurugram", "Remote") if c.lower() in resume.lower()],
        )

    def text(self, prompt: str, *, system: str | None = None) -> str:
        return "Offline mode: connect a Gemini API key to generate a tailored draft."

    def step(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> Turn:
        last = history[-1]
        if last.role == "user":
            return Turn(role="assistant", tool_calls=[ToolCall("call_0", "search_jobs", {"query": last.text, "limit": 5})])
        if last.role == "tool":
            jobs = last.tool_results[0].content.get("jobs", []) if last.tool_results else []
            if not jobs:
                return Turn(role="assistant", text="I couldn't find matching jobs. Try a broader query.")
            lines = [f"- {j['title']} at {j['company']} ({j['location'] or 'n/a'}) [{j['id']}]" for j in jobs[:5]]
            return Turn(role="assistant", text="Here are the closest matches (offline mode):\n" + "\n".join(lines))
        return Turn(role="assistant", text="(offline mode)")


class ScriptedLLM:
    """Test double: replays `turns` in order and answers structured calls via `handlers`."""

    name = "scripted"

    def __init__(self, turns: list[Turn] | None = None, handlers: dict[type, Callable[[str], Any]] | None = None):
        self.turns = list(turns or [])
        self.handlers = handlers or {}
        self.calls: list[list[Turn]] = []

    def structured(self, prompt: str, schema: type[T], *, system: str | None = None) -> T:
        return self.handlers[schema](prompt)

    def text(self, prompt: str, *, system: str | None = None) -> str:
        return "scripted text"

    def step(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> Turn:
        self.calls.append(list(history))
        if not self.turns:
            raise LLMError("ScriptedLLM ran out of turns")
        return self.turns.pop(0)
