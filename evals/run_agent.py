"""Tool-selection accuracy: does the first model step call the expected tool with the expected arguments?"""
from __future__ import annotations

import time

from app.agent.loop import system_prompt
from app.agent.tools import TOOL_SPECS, ToolContext
from app.llm import get_llm
from app.llm.base import Turn
from app.models import CandidateProfile, Preferences
from evals.common import RESULTS, ROOT, load_json, write_json

PROFILE = CandidateProfile(
    name="Test Candidate",
    headline="Final-year CS student, Android and Python backend",
    seniority="intern",
    years_experience=0.5,
    skills=["Kotlin", "Jetpack Compose", "Python", "PostgreSQL", "REST APIs", "Git"],
    target_roles=["Android Developer", "Backend Engineer"],
)


def norm(v):
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, list):
        return sorted(norm(x) for x in v)
    return v


def args_match(expected: dict, actual: dict) -> bool:
    for k, v in expected.items():
        got = actual.get(k)
        if isinstance(v, list):
            if not set(norm(v)) <= set(norm(got or [])):
                return False
        elif isinstance(v, str):
            if norm(v) not in norm(got or ""):
                return False
        elif got != v:
            return False
    return True


def main() -> None:
    llm = get_llm()
    ctx = ToolContext(conn=None, user_id="eval", llm=llm, profile=PROFILE, profile_vector=None,
                      prefs=Preferences(), charge=lambda: None)
    cases = load_json(ROOT / "agent_cases.json")
    rows, tool_ok, args_ok, latencies = [], 0, 0, []
    for c in cases:
        t0 = time.perf_counter()
        turn = llm.step(system_prompt(ctx), [Turn(role="user", text=c["message"])], TOOL_SPECS)
        latencies.append((time.perf_counter() - t0) * 1000)
        call = turn.tool_calls[0] if turn.tool_calls else None
        t_ok = call is not None and call.name == c["tool"]
        a_ok = t_ok and args_match(c["args"], call.args)
        tool_ok += t_ok
        args_ok += a_ok
        rows.append({"message": c["message"], "expected": c["tool"], "got": call.name if call else None,
                     "args": call.args if call else None, "tool_ok": t_ok, "args_ok": a_ok})
        print(f"{'OK ' if a_ok else 'BAD'} {c['tool']:<26} <- {call.name if call else '(text reply)'}")

    n = len(cases)
    summary = {"cases": n, "tool_accuracy": round(tool_ok / n, 3), "tool_and_args_accuracy": round(args_ok / n, 3),
               "mean_step_ms": round(sum(latencies) / n), "model": getattr(llm, "model", llm.name)}
    write_json(RESULTS / "agent.json", {"summary": summary, "cases": rows})
    print(f"\ntool accuracy {summary['tool_accuracy']:.0%} | tool+args {summary['tool_and_args_accuracy']:.0%} "
          f"| mean step {summary['mean_step_ms']} ms | {n} cases")


if __name__ == "__main__":
    main()
