-- Observability for the home page: a bounded activity feed and per-day response-status counts.
-- Counts, timestamps, tool names and record IDs only — never amounts, notes or request bodies.
-- Rejection reasons name a field, never a value (validation.py), so they are safe to keep here.

CREATE TABLE activity (
    id        BIGSERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL,
    kind      TEXT NOT NULL,
    device_id TEXT NULL,
    status    INTEGER NOT NULL,
    ms        REAL NULL,
    detail    JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX activity_at ON activity (at DESC, id DESC);

CREATE TABLE usage_status (
    day    DATE NOT NULL,
    status INTEGER NOT NULL,
    count  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, status)
);
