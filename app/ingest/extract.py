"""Structure pulled out of a posting's plain text at ingest: the sections that describe the role, the years of
experience it asks for, and the skills it names.

Postings put the role last: a median description is ~6,000 characters, often opening with "About <company>"
and closing with benefits and legal text. Embeddings read ~2,000 characters and the reranker less, so they
get `focus_text()`: role, responsibilities and requirements first, company boilerplate dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- sections
ROLE, DUTIES, REQS, NICE, OTHER, DROP = "role", "duties", "reqs", "nice", "other", "drop"

# Matched against a heading line, lower-cased with trailing punctuation stripped. First match wins.
_HEADINGS: list[tuple[str, re.Pattern]] = [
    (NICE, re.compile(r"\b(preferred|nice[- ]to[- ]haves?|bonus|desired|good[- ]to[- ]have|pluses|extra credit|ideally)\b")),
    (REQS, re.compile(
        r"\b(requirements?|qualifications?|who you are|about you|what you('|’)?ll? bring|what you bring|"
        r"what we('|’)?re looking for|what we look for|looking for|you might thrive|skills|must[- ]haves?|"
        r"you have|you should have|you('|’)?ll need|experience|competenc\w*|ideal candidate|is this you)\b")),
    (DUTIES, re.compile(
        r"\b(responsibilit\w*|what you('|’)?ll do|what you will do|what you('|’)?ll be doing|what you will be doing|"
        r"in this role|the impact|your impact|you('|’)?ll achieve|day[- ]to[- ]day|your role|key results?|"
        r"what you('|’)?ll work on|the work|duties|you will)\b")),
    (DROP, re.compile(
        r"\b(benefits?|perks|what we offer|we offer|compensation|salary|pay (range|transparency)|equity|"
        r"equal (opportunity|employment)|eeo|diversity|inclusion|accommodations?|privacy|compliance|"
        r"why (join|work)|life at|our (values|mission|culture|story)|culture|who we are|about us|"
        r"how we work|hiring process|interview process|recruitment fraud|logistics|travel|location|"
        r"a note on ai|supports? full-time|employee rights|disclaimer|apply|#li)\b")),
    (ROLE, re.compile(r"\b(about the (role|job|position|opportunity)|the role|role overview|overview of this role|"
                      r"job (description|summary|overview)|position (summary|overview)|the opportunity|summary)\b")),
    (OTHER, re.compile(r"\babout the team\b")),
    (DROP, re.compile(r"^about \S")),  # "About Stripe", "About OpenAI": the company pitch
]
_HEADING_MAX = 70


@dataclass
class Section:
    kind: str
    heading: str
    lines: list[str]

    @property
    def text(self) -> str:
        body = "\n".join(self.lines).strip()
        return f"{self.heading}\n{body}".strip() if self.heading else body


# Words allowed before the heading phrase on a line without a colon ("Key responsibilities", "Our benefits").
_LEAD_WORDS = {"key", "basic", "minimum", "required", "preferred", "your", "our", "the", "core", "main", "additional",
               "technical", "job", "role", "general", "essential", "desired", "annual", "base", "estimated", "total",
               "us", "united", "states", "a", "an", "and", "&", "soft"}
_BULLETS = ("- ", "• ", "· ", "– ", "* ")


def heading_kind(line: str) -> str | None:
    """The section a heading line opens, or None if the line isn't a heading.

    Lines ending in ':' are headings when short. Without a colon the heading phrase must open the line
    ("Responsibilities", "What you'll do"), so "Build compliance tooling" stays body text.
    """
    raw = line.strip()
    if not raw or len(raw) > _HEADING_MAX or raw.startswith(_BULLETS):
        return None
    ends_colon = raw.rstrip("*").endswith(":")
    if not ends_colon and re.search(r"[.!?,;]$", raw):
        return None  # a sentence, not a heading
    key = raw.lower().strip(" :*#").strip()
    words = key.split()
    if not words or len(words) > (10 if ends_colon else 9):
        return None
    best: tuple[int, str] | None = None
    for kind, pattern in _HEADINGS:  # earliest match wins; ties go to the pattern listed first
        m = pattern.search(key)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind)
    if best is None:
        return OTHER if ends_colon and len(words) <= 6 else None
    start, kind = best
    before = key[:start].split()
    if not ends_colon and (len(before) > 2 or any(w not in _LEAD_WORDS for w in before)):
        return None
    return kind


def split_sections(text: str) -> list[Section]:
    sections = [Section(OTHER, "", [])]  # preamble before the first heading
    for line in text.splitlines():
        kind = heading_kind(line)
        if kind is not None:
            sections.append(Section(kind, line.strip(), []))
        else:
            sections[-1].lines.append(line)
    return [s for s in sections if s.heading or "".join(s.lines).strip()]


_ORDER = {ROLE: 0, DUTIES: 1, REQS: 2, NICE: 3, OTHER: 4}


def focus_text(description: str, min_chars: int = 300) -> str:
    """Role-relevant sections first (role, duties, requirements, nice-to-haves, other), boilerplate dropped.

    Falls back to the original text when no headings are found or too little is left.
    """
    sections = split_sections(description or "")
    if len(sections) <= 1:
        return description or ""
    kept = sorted((s for s in sections if s.kind != DROP), key=lambda s: _ORDER[s.kind])  # stable within a kind
    out = "\n\n".join(s.text for s in kept if s.text).strip()
    return out if len(out) >= min_chars else description


# ---------------------------------------------------------------- years of experience
_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split())}
_NUM = r"(\d{1,2}(?:\.\d)?|" + "|".join(_NUM_WORDS) + r")"
_YEARS = re.compile(
    rf"(?:(?:minimum|min\.?|at least|over|more than)\s+(?:of\s+)?)?\b{_NUM}\s*(?:\+|plus)?\s*"
    rf"(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)
# What follows "N years" when it is a requirement: "of experience", "in a backend role", "building ...".
# "4-year degree", "10 years ago" and "for 20 years, we have" don't match.
_EXPERIENCE_CTX = re.compile(
    r"^\W*(?:of|in|on|as|at|with|within|across|building|developing|designing|running|operating|managing|leading|"
    r"selling|shipping|working|writing|doing|owning|delivering|supporting|using)\b|"
    r"\b(experience|exp\b|industry|professional|hands[- ]on|background|track record)", re.IGNORECASE)
_NOT_REQUIRED = re.compile(r"\b(prefer|preferred|nice to have|bonus|ideally|a plus|advantage|desirable)\b", re.I)


def _num(s: str) -> float:
    s = s.lower()
    return float(_NUM_WORDS[s]) if s in _NUM_WORDS else float(s)


def min_years(description: str) -> int | None:
    """The years of experience a posting requires ("3+ years of experience" -> 3), or None if it doesn't say.

    Skips boilerplate sections and requirements whose own clause calls them preferred. A "Preferred /
    Desired qualifications" section counts only when the posting has no requirements section: many Indian
    postings list their real requirements under "Desired". With several requirements ("5+ years overall,
    2+ in Kotlin") the largest one wins.
    """
    sections = split_sections(description or "")
    skip = {NICE, DROP} if any(s.kind == REQS for s in sections) else {DROP}
    best: float | None = None
    for section in sections:
        if section.kind in skip:
            continue
        for line in [section.heading, *section.lines]:
            matches = list(_YEARS.finditer(line or ""))
            for i, m in enumerate(matches):
                # The clause after "N years": up to the sentence end or the next "N years".
                end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
                after = re.split(r"[.;]\s", line[m.end() : end], maxsplit=1)[0][:100]
                before = line[max(0, m.start() - 40) : m.start()]
                if not (_EXPERIENCE_CTX.search(after) or re.search(r"experience\W*$", before, re.I)):
                    continue
                if _NOT_REQUIRED.search(before.rsplit(",", 1)[-1]) or _NOT_REQUIRED.search(after.split(",")[0]):
                    continue  # "3+ years of Go is a plus", "ideally 2+ years ..."
                low = _num(m.group(1))
                if 0 < low <= 15:
                    best = low if best is None else max(best, low)
    return int(best) if best is not None else None


# ---------------------------------------------------------------- skills
# Canonical name -> regex (case-insensitive unless the pattern sets (?-i)). Ambiguous words need context:
# "Go" is matched only as "Golang" or in a list, "Spring" only as Spring Boot/Framework.
_SKILL_PATTERNS: dict[str, str] = {
    # languages
    "Python": r"python", "Java": r"java(?!\s*script)", "Kotlin": r"kotlin", "JavaScript": r"javascript|\bes6\b",
    "TypeScript": r"typescript", "C++": r"c\+\+|\bcpp\b", "C#": r"c#|csharp",
    "Go": r"golang|(?-i:\bGo\b)(?=\s*(?:[,/)]|and\b|or\b))|(?:(?<=[,/(]\s)|(?<=[,/(]))(?-i:Go)\b(?!-)",
    "Rust": r"\brust\b", "Ruby": r"\bruby\b", "PHP": r"\bphp\b", "Scala": r"\bscala\b", "Swift": r"(?-i:\bSwift\b)",
    "Objective-C": r"objective[- ]c", "Dart": r"\bdart\b", "R": r"(?-i:\bR\b)(?=\s*(?:[,/)]|and\b|or\b|programming))",
    "SQL": r"\bsql\b", "Bash": r"\bbash\b|shell scripting", "Elixir": r"\belixir\b", "Solidity": r"solidity",
    # web / mobile
    "React": r"\breact(?:\.js|js)?\b(?!\s*native)", "React Native": r"react native", "Next.js": r"next\.?js",
    "Angular": r"angular(?:js)?","Vue": r"\bvue(?:\.js|js)?\b", "Node.js": r"node\.?js|\bnode\b(?=\s*[,/)])",
    "HTML/CSS": r"\bhtml5?\b|\bcss3?\b", "Tailwind": r"tailwind(?:\s?css)?", "Redux": r"\bredux\b", "GraphQL": r"graphql",
    "REST": r"(?-i:\bREST\b)|restful|rest apis?", "gRPC": r"\bgrpc\b", "Android": r"\bandroid\b", "iOS": r"\bios\b",
    "Jetpack Compose": r"jetpack compose", "SwiftUI": r"swiftui", "Flutter": r"\bflutter\b",
    # backend frameworks
    "Django": r"\bdjango\b", "Flask": r"\bflask\b", "FastAPI": r"fastapi", "Spring Boot": r"spring ?boot|spring framework",
    "Rails": r"\brails\b", "Express": r"express\.?js", "Laravel": r"laravel", ".NET": r"asp\.net|\.net\b|dotnet",
    # data stores / messaging
    "PostgreSQL": r"postgres(?:ql)?", "MySQL": r"mysql", "MongoDB": r"mongo(?:db)?", "Redis": r"\bredis\b",
    "Elasticsearch": r"elastic ?search|opensearch", "Cassandra": r"cassandra", "DynamoDB": r"dynamo ?db",
    "Kafka": r"\bkafka\b", "RabbitMQ": r"rabbitmq", "Snowflake": r"snowflake", "BigQuery": r"bigquery",
    "Spark": r"\bspark\b|pyspark", "Airflow": r"airflow", "dbt": r"(?-i:\bdbt\b)", "Hadoop": r"hadoop",
    "Firebase": r"firebase", "Supabase": r"supabase", "ClickHouse": r"clickhouse",
    # infra
    "AWS": r"\baws\b|amazon web services", "GCP": r"\bgcp\b|google cloud", "Azure": r"\bazure\b",
    "Docker": r"docker", "Kubernetes": r"kubernetes|\bk8s\b", "Terraform": r"terraform", "Linux": r"\blinux\b",
    "CI/CD": r"ci ?/ ?cd|continuous (integration|delivery|deployment)", "Git": r"\bgit\b",
    "Microservices": r"micro-?services", "Distributed Systems": r"distributed systems",
    "System Design": r"system design", "Prometheus": r"prometheus", "Grafana": r"grafana",
    # ML / data
    "Machine Learning": r"machine learning|\bml\b", "Deep Learning": r"deep learning", "NLP": r"\bnlp\b|natural language",
    "Computer Vision": r"computer vision", "LLM": r"\bllms?\b|large language model", "RAG": r"\brag\b|retrieval[- ]augmented",
    "PyTorch": r"pytorch", "TensorFlow": r"tensorflow", "scikit-learn": r"scikit|sklearn", "Pandas": r"\bpandas\b",
    "NumPy": r"numpy", "Statistics": r"statistic(?:s|al)?", "Data Analysis": r"data analy\w*", "Tableau": r"tableau",
    "Power BI": r"power ?bi",
    "Excel": r"microsoft excel|ms excel|advanced excel|(?-i:\bExcel\b)(?=\s*(?:[,/)]|and\b|or\b))", "MLOps": r"mlops", "CUDA": r"\bcuda\b",
    # practice
    "Data Structures": r"data structures", "Algorithms": r"\balgorithms?\b", "Testing": r"unit test\w*|automated test\w*|\btdd\b",
    "Figma": r"figma", "Security": r"application security|security engineering|appsec|infosec|cybersecurity",
}
SKILLS: dict[str, re.Pattern] = {
    name: re.compile(rf"(?<![\w+#.])(?:{pat})(?![\w+#])" if not pat.startswith("(?-i") else pat, re.IGNORECASE)
    for name, pat in _SKILL_PATTERNS.items()
}


def find_skills(*texts: str | None) -> list[str]:
    """Canonical skills named in the text, in vocabulary order."""
    text = "\n".join(t for t in texts if t)
    return [name for name, pattern in SKILLS.items() if pattern.search(text)]


def canonical_skills(skills: list[str]) -> list[str]:
    """Map free-text skills (from a resume) onto the vocabulary: 'Postgres' -> PostgreSQL. Unknown ones are dropped."""
    out: list[str] = []
    for s in skills:
        for name in find_skills(s):
            if name not in out:
                out.append(name)
    return out


def job_skills(title: str, description: str) -> list[str]:
    """Skills a posting asks for: title plus role-relevant sections (an 'About us' tech list doesn't count)."""
    return find_skills(title, focus_text(description))
