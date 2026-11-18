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
