import pytest

from app.llm.fake import ScriptedLLM
from app.models import Job, Preferences, RerankItem, RerankResult, ScoredJob
from app.search import or_tsquery, preference_boost, rerank, rrf
from evals.metrics import mrr, ndcg_at_k, percentile, precision_at_k, recall_at_k


def job(i, title="Engineer", location="Bengaluru", remote=False):
    return Job(id=i, source="t", board="t", company="C", title=title, location=location, is_remote=remote, url="u")


def test_rrf_rewards_agreement():
    scores = rrf([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["a"] == scores["b"]
    assert scores["a"] > scores["c"] and scores["c"] == scores["d"]


def test_or_tsquery_dedupes_and_strips_symbols():
    assert or_tsquery("Python/FastAPI, python & C++!") == "python | fastapi | c"
    assert or_tsquery("!!!") == ""


def test_preference_boost():
    prefs = Preferences(locations=["bengaluru"], roles=["backend engineer"], remote_ok=True)
    assert preference_boost(job("1", "Senior Backend Engineer"), prefs) == pytest.approx(1.25)
    assert preference_boost(job("2", "Designer", "Pune"), prefs) == 1.0
    assert preference_boost(job("3"), None) == 1.0


def test_city_filters_and_early_career_queries():
    from app.models import SearchFilters
    from app.search import filter_cities, is_early_career_query

    assert filter_cities(SearchFilters(location=" Gurgaon ")) == (["Gurugram"], None)
    assert filter_cities(SearchFilters(location="ncr", cities=["Pune"])) == (["Pune", "Delhi NCR"], None)
    assert filter_cities(SearchFilters(location="Koramangala")) == ([], "Koramangala")  # unknown: substring match
    assert is_early_career_query("software engineering internships in Bengaluru")
    assert is_early_career_query("new grad backend roles")
    assert not is_early_career_query("internal tools engineer")
    assert not is_early_career_query("senior backend engineer")


def test_preference_boost_uses_canonical_cities():
    prefs = Preferences(locations=["gurgaon"])
    gurugram = job("1", location="Sector 44").model_copy(update={"cities": ["Gurugram", "Delhi NCR"]})
    assert preference_boost(gurugram, prefs) == pytest.approx(1.15)


def test_freshness_boost_is_small_and_recent_only():
    from datetime import datetime, timedelta, timezone

    from app.search import freshness_boost

    now = datetime.now(timezone.utc)
    assert freshness_boost(job("1").model_copy(update={"posted_at": now - timedelta(days=1)}), now) == 1.05
    assert freshness_boost(job("2").model_copy(update={"posted_at": now - timedelta(days=10)}), now) == 1.0
    assert freshness_boost(job("3"), now) == 1.0  # undated


def test_dedupe_collapses_city_suffixed_reposts():
    from app.search import dedupe

    a = job("a", "Backend Engineer - Bangalore").model_copy(update={"cities": ["Bengaluru"]})
    b = job("b", "Backend Engineer (Bengaluru)").model_copy(update={"cities": ["Bengaluru"]})
    c = job("c", "Backend Engineer - Pune").model_copy(update={"cities": ["Pune"]})
    out = dedupe([ScoredJob(job=x, score=1.0) for x in (a, b, c)])
    assert [r.job.id for r in out] == ["a", "c"]


def test_title_levels():
    from app.search import title_level

    assert title_level("Senior Engineering Manager") == 4
    assert title_level("Senior Associate, Risk") == 3
    assert title_level("Software Engineer Intern") == 0
    assert title_level("Assistant Manager - Internal Audit") == 4  # 'Internal' is not 'intern'
    assert title_level("New Grad Software Engineer") == 1
    assert title_level("Backend Engineer") is None


def test_seniority_fit_prefers_level_appropriate_roles():
    from app.models import CandidateProfile
    from app.search import seniority_fit

    student = CandidateProfile(name="", headline="", seniority="intern", years_experience=0.5,
                               skills=[], target_roles=[])
    intern, new_grad = job("1", "SDE Intern"), job("2", "New Grad Engineer")
    senior, staff, plain = job("3", "Senior SDE"), job("4", "Staff Engineer"), job("5", "Backend Engineer")
    assert seniority_fit(intern, student) == seniority_fit(new_grad, student) > seniority_fit(plain, student)
    assert seniority_fit(plain, student) > seniority_fit(senior, student) > seniority_fit(staff, student)
    assert seniority_fit(staff, None) == 1.0


def test_rerank_orders_by_relevance_and_ignores_hallucinated_ids():
    cands = [ScoredJob(job=job(i), score=1.0) for i in ("a", "b", "c")]
    llm = ScriptedLLM(handlers={RerankResult: lambda p: RerankResult(items=[
        RerankItem(job_id="c", relevance=9, reason="Python match"),
        RerankItem(job_id="zzz", relevance=10, reason="made up"),
        RerankItem(job_id="a", relevance=3, reason="weak"),
    ])})
    out = rerank(llm, "python", cands, None)
    assert [o.job.id for o in out] == ["c", "a", "b"]  # b was skipped by the model -> kept, last
    assert out[0].reason == "Python match" and out[2].reason is None


def test_metrics_known_values():
    qrels = {"a": 3, "b": 2, "c": 0}
    assert ndcg_at_k(["a", "b", "c"], qrels, 3) == pytest.approx(1.0)
    assert ndcg_at_k(["c", "b", "a"], qrels, 3) < 1.0
    assert recall_at_k(["x", "b"], qrels, 2) == 0.5
    assert precision_at_k(["a", "x"], qrels, 2) == 0.5
    assert mrr(["x", "c", "b"], qrels) == pytest.approx(1 / 3)
    assert ndcg_at_k(["a"], {}, 10) == 0.0
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([1, 2, 3, 4], 95) == 4


def test_rrf_weights_scale_each_list():
    scores = rrf([["a", "b"], ["b", "c"]], k=60, weights=[0.3, 1.0])
    assert scores["a"] == pytest.approx(0.3 / 61)
    assert scores["b"] == pytest.approx(0.3 / 62 + 1 / 61)
    assert "a" not in rrf([["a"], ["b"]], weights=[0.0, 1.0])  # weight 0 drops the list


def test_experience_fit_uses_required_years():
    from app.models import CandidateProfile
    from app.search import experience_fit

    student = CandidateProfile(name="", headline="", seniority="intern", years_experience=0.5,
                               skills=[], target_roles=[])
    need = lambda n: job("x").model_copy(update={"min_years": n})  # noqa: E731
    assert experience_fit(need(None), student) == 1.0
    assert experience_fit(need(1), student) == 1.0  # a year of slack
    assert experience_fit(need(2), student) == 0.85
    assert experience_fit(need(3), student) == 0.65
    assert experience_fit(need(8), student) == 0.45
    assert experience_fit(need(8), None) == 1.0  # no profile, no level in the query
    assert experience_fit(need(8), None, target_years=0) == 0.45  # "internships ..."


def test_skill_fit_is_a_bounded_boost():
    from app.search import skill_fit

    tagged = job("x").model_copy(update={"skills": ["Python", "PostgreSQL", "Kafka", "Go"]})
    boost, shared = skill_fit(tagged, {"Python", "PostgreSQL"})
    assert shared == ["Python", "PostgreSQL"] and boost == pytest.approx(1.1)
    assert skill_fit(tagged, {"Python", "PostgreSQL", "Kafka", "Go", "Rust"})[0] == pytest.approx(1.2)
    assert skill_fit(tagged, set()) == (1.0, [])
    assert skill_fit(job("y"), {"Python"}) == (1.0, [])  # untagged posting: neutral, not penalised


def test_personal_vector_moves_toward_saved_and_away_from_dismissed():
    import numpy as np

    from app.search import personal_vector

    p, liked, disliked = np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0])
    assert personal_vector(p) is p  # no feedback: unchanged
    v = personal_vector(p, liked, disliked)
    assert np.isclose(np.linalg.norm(v), 1.0)
    assert v[0] > v[1] > 0 > v[2]  # profile still dominates


def test_profile_query_is_roles_and_top_skills():
    from app.models import CandidateProfile
    from app.search import profile_query

    p = CandidateProfile(name="", headline="", seniority="intern", years_experience=0,
                         skills=[f"s{i}" for i in range(10)], target_roles=["Android Developer"])
    assert profile_query(p, None) == "Android Developer s0 s1 s2 s3 s4 s5"
    assert profile_query(p, Preferences(roles=["Backend Engineer"])).startswith("Backend Engineer s0")


def test_rerank_batches_interleave_break_ties_by_rank_and_survive_one_failure(monkeypatch):
    from app.config import get_settings
    from app.llm.base import LLMError

    monkeypatch.setattr(get_settings(), "rerank_batch_size", 3)
    cands = [ScoredJob(job=job(str(i)), score=1.0) for i in range(6)]
    seen_batches = []

    def grade(prompt):
        import re

        ids = re.findall(r"^\[(\S+)\]", prompt, re.MULTILINE)
        seen_batches.append(ids)
        return RerankResult(items=[RerankItem(job_id=i, relevance=5, reason="same") for i in ids])

    out = rerank(ScriptedLLM(handlers={RerankResult: grade}), "q", cands, None)
    assert sorted(seen_batches) == [["0", "2", "4"], ["1", "3", "5"]]  # interleaved by rank
    assert [o.job.id for o in out] == ["0", "1", "2", "3", "4", "5"]  # equal grades keep retrieval order

    def flaky(prompt):
        if "[0]" in prompt:
            raise LLMError("503")
        return grade(prompt)

    out = rerank(ScriptedLLM(handlers={RerankResult: flaky}), "q", cands, None)
    assert [o.job.id for o in out][:3] == ["1", "3", "5"] and out[3].reason is None  # failed batch: ungraded, last

    def down(prompt):
        raise LLMError("503")

    with pytest.raises(LLMError):
        rerank(ScriptedLLM(handlers={RerankResult: down}), "q", cands, None)
