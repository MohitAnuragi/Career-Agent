"""Minimal raw payloads in the exact shapes the three public APIs return."""

GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Backend Engineer, Payments",
            "company_name": "Groww",
            "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://job-boards.greenhouse.io/groww/jobs/101",
            "first_published": "2026-10-01T10:00:00-04:00",
            "content": "&lt;p&gt;Build &lt;strong&gt;Python&lt;/strong&gt; services with PostgreSQL.&lt;/p&gt;",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Bengaluru", "location": "Bengaluru, India"}],
        },
        {
            "id": 102,
            "title": "Account Executive",
            "company_name": "Groww",
            "location": {"name": "Indianapolis, Indiana"},
            "absolute_url": "https://job-boards.greenhouse.io/groww/jobs/102",
            "content": "&lt;p&gt;Sales role.&lt;/p&gt;",
            "departments": [],
            "offices": [],
        },
    ]
}

LEVER = [
    {
        "id": "aa-1",
        "text": "Android Developer Intern",
        "categories": {"commitment": "Intern", "department": "Engineering", "team": "Mobile",
                       "location": "Bangalore", "allLocations": ["Bangalore"]},
        "country": "IN",
        "workplaceType": "onsite",
        "createdAt": 1790573982021,
        "descriptionPlain": "",
        "lists": [{"text": "What you'll do", "content": "<li>Ship Kotlin and Jetpack Compose features</li>"}],
        "additionalPlain": "Stipend provided.",
        "hostedUrl": "https://jobs.lever.co/cred/aa-1",
    }
]

ASHBY = {
    "jobs": [
        {
            "id": "ab-1",
            "title": "Applied AI Engineer",
            "department": "Engineering",
            "employmentType": "FullTime",
            "location": "Remote - India",
            "secondaryLocations": [],
            "isListed": True,
            "isRemote": True,
            "publishedAt": "2026-10-05T11:46:39.214+00:00",
            "address": {"postalAddress": {"addressCountry": "India"}},
            "jobUrl": "https://jobs.ashbyhq.com/sarvam/ab-1",
            "descriptionPlain": "Build LLM agents with tool calling, RAG and embeddings in Python.",
        },
        {
            "id": "ab-2",
            "title": "Hidden role",
            "location": "Delhi",
            "isListed": False,
            "jobUrl": "https://jobs.ashbyhq.com/sarvam/ab-2",
            "descriptionPlain": "x",
        },
    ]
}
