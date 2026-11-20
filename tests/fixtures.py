"""Minimal raw payloads in the exact shapes the public job-board APIs return.

Dates are relative to now: the app hides postings older than 14 days by default, so fixed dates would age out.
"""
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def iso(n: float) -> str:
    return days_ago(n).isoformat()


GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Backend Engineer, Payments",
            "company_name": "Groww",
            "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://job-boards.greenhouse.io/groww/jobs/101",
            "first_published": iso(2),
            "content": "&lt;p&gt;Build &lt;strong&gt;Python&lt;/strong&gt; services with PostgreSQL.&lt;/p&gt;",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Bengaluru", "location": "Bengaluru, India"}],
        },
        {
            # No first_published: updated_at must not be used as the posted date (an edit bumps it).
            "id": 102,
            "title": "Account Executive",
            "company_name": "Groww",
            "location": {"name": "Indianapolis, Indiana"},
            "absolute_url": "https://job-boards.greenhouse.io/groww/jobs/102",
            "updated_at": iso(0),
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
        "createdAt": int(days_ago(5).timestamp() * 1000),
        "descriptionPlain": "",
        "lists": [{"text": "What you'll do", "content": "<li>Ship Kotlin and Jetpack Compose features</li>"}],
        "additionalPlain": "Stipend provided.",
        "hostedUrl": "https://jobs.lever.co/cred/aa-1",
    },
    {
        # 20 days old: outside the default 14-day window, inside 30.
        "id": "aa-2",
        "text": "Data Analyst",
        "categories": {"commitment": "Full-time", "department": "Analytics",
                       "location": "Gurgaon", "allLocations": ["Gurgaon"]},
        "country": "IN",
        "workplaceType": "hybrid",
        "createdAt": int(days_ago(20).timestamp() * 1000),
        "descriptionPlain": "SQL dashboards and experimentation for the rewards team.",
        "lists": [],
        "hostedUrl": "https://jobs.lever.co/cred/aa-2",
    },
]

LEVER_US_REMOTE = [
    {
        "id": "us-1",
        "text": "Account Manager",
        "categories": {"location": "Remote", "allLocations": ["Remote"]},
        "country": "US",
        "workplaceType": "remote",
        "createdAt": int(days_ago(1).timestamp() * 1000),
        "descriptionPlain": "Manage accounts.",
        "hostedUrl": "https://jobs.lever.co/acme/us-1",
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
            "publishedAt": iso(1),
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

# Workable lists a job once per location under the same shortcode.
WORKABLE = {
    "name": "Leena AI",
    "jobs": [
        {
            "title": "Senior QA Engineer", "shortcode": "AB12CD34EF", "employment_type": "Full-time",
            "telecommuting": False, "department": "Engineering", "url": "https://apply.workable.com/j/AB12CD34EF",
            "published_on": days_ago(3).date().isoformat(), "country": "India", "city": "Gurugram",
            "locations": [{"country": "India", "countryCode": "IN", "city": "Gurugram", "region": "Haryana",
                           "hidden": False}],
            "description": "<p>Own test automation with <strong>Playwright</strong>.</p>",
        },
        {
            "title": "Senior QA Engineer", "shortcode": "AB12CD34EF", "employment_type": "Full-time",
            "telecommuting": False, "department": "Engineering", "url": "https://apply.workable.com/j/AB12CD34EF",
            "published_on": days_ago(3).date().isoformat(), "country": "India", "city": "Bengaluru",
            "locations": [{"country": "India", "countryCode": "IN", "city": "Bengaluru", "region": "Karnataka",
                           "hidden": False}],
            "description": "<p>Own test automation with <strong>Playwright</strong>.</p>",
        },
        {
            "title": "Senior Account Executive", "shortcode": "ZZ99", "employment_type": "Full-time",
            "telecommuting": True, "department": "Sales", "url": "https://apply.workable.com/j/ZZ99",
            "published_on": days_ago(2).date().isoformat(), "country": "United States", "city": "",
            "locations": [{"country": "United States", "countryCode": "US", "city": "", "region": "",
                           "hidden": False}],
            "description": "<p>Sell.</p>",
        },
    ],
}

RECRUITEE = {
    "offers": [
        {
            "id": 2832667, "slug": "platform-engineer", "title": "Platform Engineer", "status": "published",
            "location": "Pune, Maharashtra, India", "city": "Pune", "country": "India", "country_code": "IN",
            "remote": False, "department": "Engineering", "employment_type_code": "fulltime_permanent",
            "published_at": days_ago(4).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "careers_url": "https://acme.recruitee.com/o/platform-engineer",
            "locations": [{"city": "Pune", "state": "Maharashtra", "country": "India", "country_code": "IN"}],
            "description": "<p>Run Kubernetes clusters.</p>", "requirements": "<ul><li>Terraform</li></ul>",
        },
        {
            "id": 2, "slug": "draft", "title": "Draft", "status": "draft", "location": "Pune",
            "careers_url": "https://acme.recruitee.com/o/draft", "description": "", "requirements": "",
        },
    ]
}

# Posting *detail* objects (GET /v1/companies/{id}/postings/{postingId}).
SMARTRECRUITERS = [
    {
        "id": "744000162469181",
        "name": "Site Reliability Engineer",
        "releasedDate": iso(1).replace("+00:00", "Z"),
        "location": {"city": "Hyderabad", "country": "in", "remote": False, "fullLocation": "Hyderabad, , India"},
        "department": {},
        "function": {"id": "engineering", "label": "Engineering"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "postingUrl": "https://jobs.smartrecruiters.com/Freshworks/744000162469181-site-reliability-engineer",
        "jobAd": {"sections": {
            "companyDescription": {"title": "Company", "text": "<p>About us</p>"},
            "jobDescription": {"title": "Job", "text": "<p>Keep services up with Prometheus.</p>"},
            "qualifications": {"title": "Qualifications", "text": "<ul><li>Linux</li></ul>"},
            "additionalInformation": {"title": "More", "text": ""},
        }},
    }
]
