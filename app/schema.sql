CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- One row per job posting. `id` is "<source>:<company>:<external id>" so it is stable across runs.
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,            -- greenhouse | lever | ashby
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

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb
);
