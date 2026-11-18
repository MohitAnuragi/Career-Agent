"""The agent loop: model step -> execute tool calls -> feed results back, until a final answer or the step budget."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from app.agent.tools import TOOL_SPECS, ToolContext, ToolError, dispatch
from app.llm.base import LLM, ToolResult, Turn

log = logging.getLogger("agent")
MAX_STEPS = 6


def system_prompt(ctx: ToolContext) -> str:
    profile = ctx.profile.model_dump_json() if ctx.profile else "none (resume not uploaded yet)"
    prefs = ctx.prefs.model_dump_json(exclude_defaults=True) or "{}"
    return f"""You are Career Agent, a job-search assistant for students and early-career engineers.
Today is {date.today().isoformat()}.

Candidate profile: {profile}
Remembered preferences: {prefs}

Rules:
- Every job, company, link or requirement you mention must come from a tool result in this conversation. Never invent postings.
- Use recommend_jobs for "what should I apply to" style requests and search_jobs for specific queries.
- When the user states a lasting preference (city, remote, roles, companies to avoid), call update_preferences.
- Before claiming the user is a good fit for a specific job, call explain_match.
- If a tool returns an error, fix the arguments or tell the user what is needed.
- Reply concisely in Markdown. List jobs as: **Title** at Company (Location) followed by the link."""


@dataclass
class AgentResult:
    reply: str
    trace: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    steps: int = 0


def run_agent(llm: LLM, ctx: ToolContext, past: list[dict], message: str, max_steps: int = MAX_STEPS) -> AgentResult:
    history = [Turn(role=m["role"], text=m["content"]) for m in past]
    history.append(Turn(role="user", text=message))
    trace: list[dict] = []

    for step in range(1, max_steps + 1):
        ctx.charge()
        turn = llm.step(system_prompt(ctx), history, TOOL_SPECS)
        history.append(turn)
        if not turn.tool_calls:
            return _result(turn.text or "(no reply)", trace, ctx, step)

        results = []
        for call in turn.tool_calls:
            started = time.perf_counter()
            try:
                out = dispatch(ctx, call.name, call.args)
                ok = True
            except ToolError as e:
                out, ok = {"error": str(e)}, False
            except Exception as e:  # keep the loop alive; the model sees a generic error
                log.exception("tool %s crashed", call.name)
                out, ok = {"error": f"{call.name} failed: {type(e).__name__}"}, False
            trace.append({
                "step": step,
                "tool": call.name,
                "args": call.args,
                "ok": ok,
                "ms": round((time.perf_counter() - started) * 1000),
                "result": _summarise(out),
            })
            results.append(ToolResult(call_id=call.id, name=call.name, content=out))
        history.append(Turn(role="tool", tool_results=results))

    # Budget exhausted: one last call without tools forces a written answer from what was gathered.
    ctx.charge()
    history.append(Turn(role="user", text="Step limit reached. Answer now using only the tool results above."))
    final = llm.step(system_prompt(ctx), history, [])
    return _result(final.text or "I ran out of steps. Please try a narrower request.", trace, ctx, max_steps)


def _summarise(out: dict) -> str:
    if "error" in out:
        return out["error"]
    if "jobs" in out:
        return f"{out.get('count', len(out['jobs']))} jobs"
    return ", ".join(sorted(out))[:120]


def _result(reply: str, trace: list[dict], ctx: ToolContext, steps: int) -> AgentResult:
    jobs = [
        {"id": j.id, "title": j.title, "company": j.company, "location": j.location, "url": j.url, "remote": j.is_remote}
        for j in ctx.seen_jobs.values()
    ]
    return AgentResult(reply=reply, trace=trace, jobs=jobs, steps=steps)
