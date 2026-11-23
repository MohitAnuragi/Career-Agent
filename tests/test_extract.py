"""Sections, years of experience and skills pulled from posting text at ingest."""
from app.ingest.extract import (
    DROP, DUTIES, NICE, REQS, ROLE, canonical_skills, find_skills, focus_text, heading_kind, job_skills, min_years,
    split_sections,
)

POSTING = """About Acme
Acme is the leading platform for widgets, trusted by 10,000 companies for over 20 years of growth.

About the role
You'll build the payments backend.

What you'll do:
- Design REST APIs in Python and Go, deployed on Kubernetes
- Own PostgreSQL schemas

Requirements
- 3+ years of experience building backend services
- Bachelor's degree (4-year) and strong SQL

Nice to have
- 7+ years of experience with Kafka

Benefits
- Health insurance, Rust-free snacks

Equal opportunity
Acme is an equal opportunity employer."""


def test_heading_kind():
    assert heading_kind("Requirements") == REQS
    assert heading_kind("Key Responsibilities:") == DUTIES
    assert heading_kind("What you’ll do") == DUTIES
    assert heading_kind("About the role") == ROLE
    assert heading_kind("About Stripe") == DROP
    assert heading_kind("Preferred qualifications") == NICE
    assert heading_kind("You might thrive in this role if you") == REQS
    # body text that happens to contain a heading word
    assert heading_kind("Build compliance tooling") is None
    assert heading_kind("- 3+ years of experience") is None
    assert heading_kind("We value great benefits and a fun culture.") is None


def test_focus_text_puts_role_first_and_drops_boilerplate():
    text = focus_text(POSTING)
    assert text.startswith("About the role")
    assert text.index("What you'll do") < text.index("Requirements") < text.index("Nice to have")
    for gone in ("trusted by 10,000", "Health insurance", "equal opportunity employer"):
        assert gone not in text
    assert focus_text("Just one paragraph, no headings.") == "Just one paragraph, no headings."
    kinds = [s.kind for s in split_sections(POSTING)]
    assert kinds == [DROP, ROLE, DUTIES, REQS, NICE, DROP, DROP]


def test_min_years():
    assert min_years(POSTING) == 3  # nice-to-have 7 years, the degree and the company's 20 years don't count
    assert min_years("Requirements\n- 5-7 years of professional experience\n- 2+ years in Kotlin") == 5
    assert min_years("Requirements\n- Two years of hands-on experience with React") == 2
    assert min_years("Requirements\n- 3+ years of Go is a plus\n- Strong CS fundamentals") is None
    assert min_years("Requirements\n- 2-3 years outbound B2B sales experience") == 2
    assert min_years("Requirements\n- A 4-year degree in CS") is None
    assert min_years("Requirements\n- 0-1 years of experience") is None  # zero: no bar to clear
    # No requirements section: a "Desired" section is the requirements
    assert min_years("About the role\nSell ads.\n\nDesired Qualifications\n-14+ years of experience in sales") == 14
    assert min_years("") is None


def test_find_skills_avoids_common_false_positives():
    assert find_skills("Python, Go and Kubernetes; REST APIs; JavaScript") == [  # vocabulary order
        "Python", "JavaScript", "Go", "REST", "Kubernetes"]
    assert find_skills("Partner with Go-to-Market teams") == []
    assert find_skills("You will excel at helping the rest of the team") == []
    assert find_skills("Java services") == ["Java"]
    assert find_skills("Built on Postgres and k8s with Node.js") == ["Node.js", "PostgreSQL", "Kubernetes"]
    assert find_skills("C++ and C# engineers") == ["C++", "C#"]


def test_skills_from_role_sections_only():
    skills = job_skills("Backend Engineer", POSTING)
    assert {"Python", "Go", "REST", "Kubernetes", "PostgreSQL", "SQL", "Kafka"} <= set(skills)
    assert "Rust" not in skills  # only mentioned under Benefits
    assert canonical_skills(["postgres", "REST APIs", "Leadership", "k8s"]) == ["PostgreSQL", "REST", "Kubernetes"]
