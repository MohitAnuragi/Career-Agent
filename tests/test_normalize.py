from app.ingest.sources import normalize_ashby, normalize_greenhouse, normalize_lever
from app.ingest.text import content_hash, html_to_text, is_india, is_remote
from tests.fixtures import ASHBY, GREENHOUSE, LEVER


def test_html_to_text_unescapes_greenhouse_entities():
    assert html_to_text("&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;", escaped=True) == "Hello world"
    assert html_to_text("<p>Intro</p><ul><li>Kotlin</li><li>Compose</li></ul>") == "Intro\n- Kotlin\n- Compose"
    assert html_to_text(None) == ""


def test_india_detection_uses_word_boundaries():
    assert is_india("Bengaluru-VTP, India")
    assert is_india("Gurgaon")
    assert not is_india("Indianapolis, Indiana")
    assert not is_india("Remote - US")


def test_remote_detection():
    assert is_remote("Remote - India")
    assert not is_remote("Mumbai")


def test_greenhouse():
    jobs = normalize_greenhouse(GREENHOUSE, "groww", "Groww")
    assert [j.id for j in jobs] == ["greenhouse:groww:101", "greenhouse:groww:102"]
    be = jobs[0]
    assert be.is_india and not be.is_remote
    assert be.department == "Engineering"
    assert "Python" in be.description and "<" not in be.description
    assert be.posted_at is not None
    assert not jobs[1].is_india  # Indiana


def test_lever_merges_lists_and_parses_epoch():
    [job] = normalize_lever(LEVER, "cred", "CRED")
    assert job.id == "lever:cred:aa-1"
    assert job.is_india
    assert "Jetpack Compose" in job.description and "Stipend" in job.description
    assert job.posted_at.year == 2026
    assert job.employment_type == "Intern"


def test_ashby_skips_unlisted_and_flags_remote():
    jobs = normalize_ashby(ASHBY, "sarvam", "Sarvam AI")
    assert [j.id for j in jobs] == ["ashby:sarvam:ab-1"]
    assert jobs[0].is_remote and jobs[0].is_india


def test_content_hash_is_stable_and_sensitive():
    assert content_hash("a", 1, None) == content_hash("a", 1, None)
    assert content_hash("a", "b") != content_hash("ab", "")  # field separator prevents collisions
