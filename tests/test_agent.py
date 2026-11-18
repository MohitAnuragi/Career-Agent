import pytest

import app.agent.loop as loop
from app.agent.tools import TOOL_SPECS, ToolContext, ToolError, dispatch
from app.llm.base import ToolCall, Turn
from app.llm.fake import ScriptedLLM
from app.models import Preferences
from app.store import RateLimited


def make_ctx(llm, charges):
    return ToolContext(conn=None, user_id="u1", llm=llm, profile=None, profile_vector=None,
                       prefs=Preferences(), charge=lambda: charges.append(1))


def test_tool_call_then_answer(monkeypatch):
    monkeypatch.setattr(loop, "dispatch", lambda ctx, name, args: {"count": 1, "jobs": [{"id": "j1"}]})
    llm = ScriptedLLM([
        Turn("assistant", tool_calls=[ToolCall("c1", "search_jobs", {"query": "android intern"})]),
        Turn("assistant", text="Found one: **Android Intern** at CRED"),
    ])
    charges = []
    res = loop.run_agent(llm, make_ctx(llm, charges), [], "find android internships")

    assert res.reply.startswith("Found one")
    assert res.steps == 2 and len(charges) == 2
    assert res.trace[0]["tool"] == "search_jobs" and res.trace[0]["ok"] and res.trace[0]["result"] == "1 jobs"
    second_call_history = llm.calls[1]
    assert second_call_history[-1].role == "tool"
    assert second_call_history[-1].tool_results[0].content["jobs"][0]["id"] == "j1"


def test_tool_errors_are_returned_to_the_model(monkeypatch):
    def boom(ctx, name, args):
        raise ToolError("Unknown job id 'x'")

    monkeypatch.setattr(loop, "dispatch", boom)
    llm = ScriptedLLM([
        Turn("assistant", tool_calls=[ToolCall("c1", "save_job", {"job_id": "x"})]),
        Turn("assistant", text="That id doesn't exist."),
    ])
    res = loop.run_agent(llm, make_ctx(llm, []), [], "save x")
    assert not res.trace[0]["ok"]
    assert llm.calls[1][-1].tool_results[0].content == {"error": "Unknown job id 'x'"}


def test_step_budget_forces_final_answer(monkeypatch):
    monkeypatch.setattr(loop, "dispatch", lambda ctx, name, args: {"ok": True})
    looping = [Turn("assistant", tool_calls=[ToolCall(f"c{i}", "list_saved_jobs", {})]) for i in range(3)]
    llm = ScriptedLLM(looping + [Turn("assistant", text="Summary")])
    res = loop.run_agent(llm, make_ctx(llm, []), [], "hi", max_steps=3)
    assert res.reply == "Summary" and len(res.trace) == 3


def test_rate_limit_stops_the_loop():
    def charge():
        raise RateLimited("limit")

    llm = ScriptedLLM([Turn("assistant", text="never")])
    ctx = ToolContext(conn=None, user_id="u", llm=llm, profile=None, profile_vector=None,
                      prefs=Preferences(), charge=charge)
    with pytest.raises(RateLimited):
        loop.run_agent(llm, ctx, [], "hi")


def test_dispatch_validates_arguments():
    ctx = make_ctx(None, [])
    with pytest.raises(ToolError, match="limit"):
        dispatch(ctx, "search_jobs", {"query": "x", "limit": 99})
    with pytest.raises(ToolError, match="Unknown tool"):
        dispatch(ctx, "delete_everything", {})
    with pytest.raises(ToolError, match="resume"):
        dispatch(ctx, "explain_match", {"job_id": "a"})


def test_tool_specs_are_clean_json_schema():
    names = {s.name for s in TOOL_SPECS}
    assert {"search_jobs", "recommend_jobs", "update_preferences", "explain_match"} <= names
    search = next(s for s in TOOL_SPECS if s.name == "search_jobs")
    assert search.parameters["type"] == "object"
    assert "query" in search.parameters["required"]
    assert "title" not in search.parameters["properties"]["query"]
