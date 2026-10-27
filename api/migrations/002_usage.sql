-- Operational metrics for the home page (served only via the authenticated GET /v1/stats).
-- Counters are batched in memory and flushed here; none of this is synced to the phone.
-- DELETE /v1/account truncates these tables too ("every row in every table").

CREATE TABLE usage_daily (
    day    DATE   NOT NULL,
    bucket TEXT   NOT NULL CHECK (bucket IN ('api', 'mcp')),
    count  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, bucket)
);

-- Most recent successful sync contact of each kind.
CREATE TABLE sync_status (
    kind      TEXT PRIMARY KEY CHECK (kind IN ('push', 'pull', 'snapshot')),
    at        TIMESTAMPTZ NOT NULL,
    device_id TEXT NULL
);
