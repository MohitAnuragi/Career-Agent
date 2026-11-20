from datetime import timedelta

from app.ingest.cities import canonical_cities, canonical_list, extract_cities
from app.ingest.pipeline import drop_reason
from app.ingest.sources import (
    _parse_dt,
    normalize_ashby,
    normalize_greenhouse,
    normalize_lever,
    normalize_recruitee,
    normalize_smartrecruiters,
    normalize_workable,
)
from app.ingest.text import content_hash, html_to_text, is_india, is_remote, remote_scope
from tests.fixtures import (
    ASHBY,
    GREENHOUSE,
    LEVER,
    LEVER_US_REMOTE,
    NOW,
    RECRUITEE,
    SMARTRECRUITERS,
    WORKABLE,
    days_ago,
)


def test_html_to_text_unescapes_greenhouse_entities():
    assert html_to_text("&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;", escaped=True) == "Hello world"
    assert html_to_text("<p>Intro</p><ul><li>Kotlin</li><li>Compose</li></ul>") == "Intro\n- Kotlin\n- Compose"
    assert html_to_text(None) == ""


def test_india_detection_uses_word_boundaries():
    assert is_india("Bengaluru-VTP, India")
    assert is_india("Gurgaon")
    assert is_india("Navi Mumbai")
    assert not is_india("Indianapolis, Indiana")
    assert not is_india("Remote - US")


def test_remote_detection():
    assert is_remote("Remote - India")
    assert not is_remote("Mumbai")


def test_remote_scope():
    assert remote_scope(False, "Bengaluru") == ""
    assert remote_scope(True, "Remote - India") == "india"
    assert remote_scope(True, "Remote (IST hours)") == "india"
    assert remote_scope(True, "Remote", country="IN") == "india"
    assert remote_scope(True, "Remote, Anywhere") == "global"
    assert remote_scope(True, "Remote - US") == "restricted"
    assert remote_scope(True, "Remote - EMEA") == "restricted"
    assert remote_scope(True, "Remote", country="US") == "restricted"
    assert remote_scope(True, "Remote") == "unspecified"
    assert remote_scope(True, "Remote (Full-time)") == "unspecified"
    assert remote_scope(True, "Arizona", "Remote") == "restricted"  # a named place that isn't India
    assert remote_scope(True, "Remote - Ontario") == "restricted"


def test_extract_cities_aliases_and_regions():
    assert extract_cities("Gurgaon, Haryana") == ["Gurugram", "Delhi NCR"]
    assert extract_cities("Bangalore; Hyderabad") == ["Bengaluru", "Hyderabad"]
    assert extract_cities("New Delhi") == ["Delhi", "Delhi NCR"]
    assert extract_cities("Delhi NCR") == ["Delhi NCR"]
    assert extract_cities("Navi Mumbai") == ["Mumbai"]
    assert extract_cities("Indianapolis, Indiana", None, "") == []


def test_canonical_cities_for_user_input():
    assert canonical_cities("gurgaon") == ["Gurugram"]
    assert canonical_cities(" NCR ") == ["Delhi NCR"]
    assert canonical_cities("delhi ncr") == ["Delhi NCR"]
    assert canonical_cities("Bangalore, India") == ["Bengaluru"]
    assert canonical_cities("Paris") == []
    assert canonical_list(["bengaluru", "Bangalore", "gurgaon", "Paris"]) == ["Bengaluru", "Gurugram", "Paris"]


def test_parse_dt_formats():
    assert _parse_dt("2026-11-18 16:23:32 UTC").tzinfo is not None  # Recruitee
    assert _parse_dt("2026-11-18").tzinfo is not None  # Workable: date only
    assert _parse_dt("2026-11-19T09:34:39.506Z").year == 2026  # SmartRecruiters
    assert _parse_dt("not a date") is None and _parse_dt(None) is None


def test_greenhouse():
    jobs = normalize_greenhouse(GREENHOUSE, "groww", "Groww")
    assert [j.id for j in jobs] == ["greenhouse:groww:101", "greenhouse:groww:102"]
    be = jobs[0]
    assert be.is_india and not be.is_remote
    assert be.cities == ["Bengaluru"]
    assert be.department == "Engineering"
    assert "Python" in be.description and "<" not in be.description
    assert be.posted_at is not None
    assert not jobs[1].is_india and jobs[1].cities == []  # Indiana
    assert jobs[1].posted_at is None  # updated_at is ignored: an edit would make an old job look new


def test_lever_merges_lists_and_parses_epoch():
    job, analyst = normalize_lever(LEVER, "cred", "CRED")
    assert job.id == "lever:cred:aa-1"
    assert job.is_india and job.cities == ["Bengaluru"]
    assert "Jetpack Compose" in job.description and "Stipend" in job.description
    assert abs(job.posted_at - days_ago(5)) < timedelta(seconds=1)
    assert job.employment_type == "Intern"
    assert analyst.cities == ["Gurugram", "Delhi NCR"]


def test_lever_us_remote_is_restricted():
    [job] = normalize_lever(LEVER_US_REMOTE, "acme", "Acme")
    assert job.is_remote and not job.is_india and job.remote_scope == "restricted"


def test_ashby_skips_unlisted_and_flags_remote():
    jobs = normalize_ashby(ASHBY, "sarvam", "Sarvam AI")
    assert [j.id for j in jobs] == ["ashby:sarvam:ab-1"]
    assert jobs[0].is_remote and jobs[0].is_india and jobs[0].remote_scope == "india"


def test_workable_merges_one_job_listed_per_location():
    qa, ae = normalize_workable(WORKABLE, "leena-ai", "Leena AI")
    assert qa.id == "workable:leena-ai:AB12CD34EF"
    assert qa.cities == ["Gurugram", "Bengaluru", "Delhi NCR"]
    assert qa.is_india and not qa.is_remote
    assert "Playwright" in qa.description and qa.posted_at is not None
    assert ae.is_remote and not ae.is_india and ae.remote_scope == "restricted"


def test_recruitee():
    [job] = normalize_recruitee(RECRUITEE, "acme", "Acme")  # the draft offer is skipped
    assert job.id == "recruitee:acme:2832667"
    assert job.cities == ["Pune"] and job.is_india
    assert "Kubernetes" in job.description and "Terraform" in job.description
    assert job.url.endswith("/o/platform-engineer") and job.posted_at is not None


def test_smartrecruiters():
    [job] = normalize_smartrecruiters(SMARTRECRUITERS, "Freshworks", "Freshworks")
    assert job.id == "smartrecruiters:Freshworks:744000162469181"
    assert job.location == "Hyderabad, India" and job.cities == ["Hyderabad"] and job.is_india
    assert "Prometheus" in job.description and "About us" not in job.description
    assert job.department == "Engineering" and job.employment_type == "Full-time"


def test_drop_reason():
    cutoff = NOW - timedelta(days=30)
    fresh, analyst = normalize_lever(LEVER, "cred", "CRED")
    assert drop_reason(fresh, all_locations=False, cutoff=cutoff) is None
    old = fresh.model_copy(update={"posted_at": days_ago(40)})
    assert drop_reason(old, all_locations=False, cutoff=cutoff) == "too_old"
    [us] = normalize_lever(LEVER_US_REMOTE, "acme", "Acme")
    assert drop_reason(us, all_locations=False, cutoff=cutoff) == "restricted_remote"
    assert drop_reason(us, all_locations=True, cutoff=cutoff) is None
    indiana = normalize_greenhouse(GREENHOUSE, "groww", "Groww")[1]
    assert drop_reason(indiana, all_locations=False, cutoff=cutoff) == "location"
    assert drop_reason(analyst.model_copy(update={"url": ""}), all_locations=False, cutoff=cutoff) == "incomplete"


def test_content_hash_is_stable_and_sensitive():
    assert content_hash("a", 1, None) == content_hash("a", 1, None)
    assert content_hash("a", "b") != content_hash("ab", "")  # field separator prevents collisions
