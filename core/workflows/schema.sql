-- Durable workflow journal schema (C2; docs/durable-workflows-plan.md §4).
-- Applied idempotently on every open; the PRAGMAs are connection-safe.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id            TEXT PRIMARY KEY,          -- 'wf-' + uuid4 hex[:12]
    spec_sha256       TEXT NOT NULL,             -- pin(spec): the configuration is identifiable
    spec_json         TEXT NOT NULL,             -- frozen copy of the spec (JSON text)
    context_id        TEXT,                      -- scoping
    user_id           TEXT,                      -- scoping
    status            TEXT NOT NULL DEFAULT 'queued',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    deadline_at       TEXT,
    queue_deadline_at TEXT,
    budget_usd_micros INTEGER,                   -- integer micro-USD or NULL (no budget)
    spent_usd_micros INTEGER NOT NULL DEFAULT 0,
    final_report      TEXT,
    owner             TEXT,                      -- lease holder; forbids double execution
    lease_expires_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_scope
    ON workflow_runs (user_id, context_id, status, created_at);

CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id       TEXT NOT NULL,
    step_id      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    result_ref   TEXT,                          -- pointer to evidence, never trusted model text
    error_code   TEXT,
    error_detail TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    PRIMARY KEY (run_id, step_id),
    FOREIGN KEY (run_id) REFERENCES workflow_runs (run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_status ON workflow_steps (run_id, status);

CREATE TABLE IF NOT EXISTS step_attempts (
    run_id          TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,            -- 1..n, allocated by begin_attempt
    outcome         TEXT NOT NULL DEFAULT 'started',  -- started|completed|failed|timed_out|interrupted
    effect_status   TEXT NOT NULL DEFAULT 'none',    -- none|unknown|confirmed
    idempotency_key TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    PRIMARY KEY (run_id, step_id, attempt),
    FOREIGN KEY (run_id) REFERENCES workflow_runs (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,  -- append-only; never rewritten
    run_id   TEXT NOT NULL,
    at       TEXT NOT NULL,
    event    TEXT NOT NULL,
    payload  TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_run ON workflow_events (run_id, seq);
