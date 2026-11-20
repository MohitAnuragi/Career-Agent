CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- One row per job posting. `id` is "<source>:<company>:<external id>" so it is stable across runs.
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,            -- greenhouse | lever | ashby | workable | smartrecruiters | recruitee
    board           TEXT NOT NULL,            -- the company's board slug on that source
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    is_remote       BOOLEAN NOT NULL DEFAULT FALSE,
    is_india        BOOLEAN NOT NULL DEFAULT FALSE,
    employment_type TEXT NOT NULL DEFAULT '',
    department      TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL,            -- sha256 of the fields that affect search
    embedding       vector(384),              -- NULL until embedded
    search_tsv      tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', title), 'A') ||
                        setweight(to_tsvector('english', company || ' ' || department), 'B') ||
                        setweight(to_tsvector('english', left(description, 20000)), 'C')
                    ) STORED,
    posted_at       TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS jobs_tsv_idx       ON jobs USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS jobs_embedding_idx ON jobs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS jobs_active_idx    ON jobs (is_active, is_india, is_remote);
CREATE INDEX IF NOT EXISTS jobs_board_idx     ON jobs (source, board);

-- Freshness, cities and remote scope (added later; ADD COLUMN IF NOT EXISTS migrates existing databases).
-- effective_posted_at: the ATS posted date, else first_seen_at when the board was already known, else NULL.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS effective_posted_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS posted_at_source TEXT NOT NULL DEFAULT 'unknown';  -- ats | first_seen | unknown
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cities TEXT[] NOT NULL DEFAULT '{}';               -- canonical, incl. "Delhi NCR"
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS remote_scope TEXT NOT NULL DEFAULT '';             -- '' | india | global | unspecified | restricted
CREATE INDEX IF NOT EXISTS jobs_fresh_idx  ON jobs (is_active, effective_posted_at DESC);
CREATE INDEX IF NOT EXISTS jobs_cities_idx ON jobs USING gin (cities);

-- Runtime state per board. boards.json stays the reviewed list of boards; this table tracks health and
-- first_ingested_at, which decides whether first_seen_at is a trustworthy posted date.
CREATE TABLE IF NOT EXISTS boards (
    source               TEXT NOT NULL,
    board                TEXT NOT NULL,
    company              TEXT NOT NULL,
    first_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at           TIMESTAMPTZ,
    last_error           TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    disabled             BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (source, board)
);

-- Anonymous users: the browser keeps the id. Only the structured profile is stored, never the resume file.
CREATE TABLE IF NOT EXISTS users (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile           JSONB,
    profile_embedding vector(384),
    preferences       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- long-term memory, editable by the agent
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Explicit feedback drives personalisation (dismissed jobs are hidden, saved jobs are listed).
CREATE TABLE IF NOT EXISTS feedback (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    action     TEXT NOT NULL CHECK (action IN ('save', 'dismiss')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id)
);

-- Short-term memory: the visible chat transcript (user + assistant text only).
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_user_idx ON messages (user_id, id);

-- Per-user daily LLM call counter (cost control).
CREATE TABLE IF NOT EXISTS llm_usage (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     DATE NOT NULL,
    calls   INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Shared daily budget across all users: anonymous sign-up is free, so a per-user cap alone can be bypassed.
CREATE TABLE IF NOT EXISTS llm_usage_global (
    day   DATE PRIMARY KEY,
    calls INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb
);
