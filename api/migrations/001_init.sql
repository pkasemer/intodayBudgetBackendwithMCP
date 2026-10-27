-- InToday backend schema v1 (backend spec §4).
-- Money is INTEGER (whole UGX). Calendar days are DATE; sync bookkeeping is TIMESTAMPTZ.
--
-- `seq` is a server-assigned, monotonically increasing position stamped on every
-- insert/update of a synced row. Pull cursors encode a seq, so they follow the server's
-- clock of record, not the writer's `updated_at` (which the client controls and which
-- can legitimately go backwards). Writers are serialised by an advisory lock in the app,
-- so commit order equals seq order.

CREATE SEQUENCE sync_seq;

CREATE FUNCTION set_sync_seq() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.seq := nextval('sync_seq');
    RETURN NEW;
END
$$;

CREATE TABLE month_plans (
    id                UUID PRIMARY KEY,
    year              INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    month             INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    buffer_amount     INTEGER NOT NULL CHECK (buffer_amount BETWEEN 0 AND 9999999),
    start_day         INTEGER NOT NULL CHECK (start_day BETWEEN 1 AND 31),
    plan_state        TEXT NOT NULL CHECK (plan_state IN ('draft', 'active', 'closed')),
    rollover_decision TEXT NULL CHECK (rollover_decision IN ('savings', 'nextMonth', 'released')),
    updated_at        TIMESTAMPTZ NOT NULL,
    device_id         TEXT NOT NULL,
    deleted_at        TIMESTAMPTZ NULL,
    seq               BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX month_plans_year_month_live ON month_plans (year, month) WHERE deleted_at IS NULL;
CREATE INDEX month_plans_year_month ON month_plans (year, month);

CREATE TABLE savings_goals (
    id            UUID PRIMARY KEY,
    name          TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    target_amount INTEGER NOT NULL CHECK (target_amount BETWEEN 1 AND 9999999),
    updated_at    TIMESTAMPTZ NOT NULL,
    device_id     TEXT NOT NULL,
    deleted_at    TIMESTAMPTZ NULL,
    seq           BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE bill_templates (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    default_amount  INTEGER NOT NULL CHECK (default_amount BETWEEN 1 AND 9999999),
    default_due_day INTEGER NOT NULL CHECK (default_due_day BETWEEN 1 AND 31),
    is_active       BOOLEAN NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    device_id       TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NULL,
    seq             BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE bills (
    id          UUID PRIMARY KEY,
    plan_id     UUID NOT NULL REFERENCES month_plans (id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    amount      INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    due_day     INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    is_paid     BOOLEAN NOT NULL,
    is_one_off  BOOLEAN NOT NULL,
    template_id UUID NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    device_id   TEXT NOT NULL,
    deleted_at  TIMESTAMPTZ NULL,
    seq         BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE expenses (
    id         UUID PRIMARY KEY,
    plan_id    UUID NOT NULL REFERENCES month_plans (id) ON DELETE CASCADE,
    amount     INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    date       DATE NOT NULL,
    tag        TEXT NOT NULL CHECK (tag IN ('food', 'transport', 'dataAirtime', 'fun', 'shopping', 'other')),
    note       TEXT NULL CHECK (length(note) <= 200),
    updated_at TIMESTAMPTZ NOT NULL,
    device_id  TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NULL,
    seq        BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE income_events (
    id              UUID PRIMARY KEY,
    plan_id         UUID NOT NULL REFERENCES month_plans (id) ON DELETE CASCADE,
    label           TEXT NOT NULL CHECK (length(btrim(label)) > 0 AND length(label) <= 100),
    amount          INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    date            DATE NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('primary', 'extra')),
    status          TEXT NOT NULL CHECK (status IN ('expected', 'received')),
    destination     TEXT NOT NULL CHECK (destination IN ('spendable', 'lockedSavings')),
    savings_goal_id UUID NULL REFERENCES savings_goals (id),
    note            TEXT NULL CHECK (length(note) <= 200),
    updated_at      TIMESTAMPTZ NOT NULL,
    device_id       TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NULL,
    seq             BIGINT NOT NULL DEFAULT 0,
    CHECK ((destination = 'lockedSavings' AND savings_goal_id IS NOT NULL)
        OR (destination = 'spendable' AND savings_goal_id IS NULL))
);

CREATE TABLE savings_allocations (
    id         UUID PRIMARY KEY,
    plan_id    UUID NOT NULL REFERENCES month_plans (id) ON DELETE CASCADE,
    goal_id    UUID NOT NULL REFERENCES savings_goals (id),
    amount     INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    updated_at TIMESTAMPTZ NOT NULL,
    device_id  TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NULL,
    seq        BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX savings_allocations_plan_goal_live
    ON savings_allocations (plan_id, goal_id) WHERE deleted_at IS NULL;

-- App-pushed computed state; opaque to the server (backend spec §6).
CREATE TABLE snapshots (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             TEXT NOT NULL CHECK (kind IN ('today', 'insights')),
    plan_year        INTEGER NOT NULL,
    plan_month       INTEGER NOT NULL CHECK (plan_month BETWEEN 1 AND 12),
    payload          JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    snapshot_version INTEGER NOT NULL CHECK (snapshot_version >= 1),
    updated_at       TIMESTAMPTZ NOT NULL,
    device_id        TEXT NOT NULL,
    seq              BIGINT NOT NULL DEFAULT 0,
    UNIQUE (kind, plan_year, plan_month)
);

CREATE TABLE idempotency_keys (
    key        TEXT PRIMARY KEY,
    response   JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pull queries, per backend spec §4 (plus seq, which the cursor actually walks).
CREATE INDEX month_plans_updated_at ON month_plans (updated_at);
CREATE INDEX savings_goals_updated_at ON savings_goals (updated_at);
CREATE INDEX bill_templates_updated_at ON bill_templates (updated_at);
CREATE INDEX bills_updated_at ON bills (updated_at);
CREATE INDEX expenses_updated_at ON expenses (updated_at);
CREATE INDEX income_events_updated_at ON income_events (updated_at);
CREATE INDEX savings_allocations_updated_at ON savings_allocations (updated_at);

CREATE INDEX bills_plan_updated ON bills (plan_id, updated_at);
CREATE INDEX expenses_plan_updated ON expenses (plan_id, updated_at);
CREATE INDEX income_events_plan_updated ON income_events (plan_id, updated_at);
CREATE INDEX savings_allocations_plan_updated ON savings_allocations (plan_id, updated_at);

CREATE INDEX month_plans_seq ON month_plans (seq);
CREATE INDEX savings_goals_seq ON savings_goals (seq);
CREATE INDEX bill_templates_seq ON bill_templates (seq);
CREATE INDEX bills_seq ON bills (seq);
CREATE INDEX expenses_seq ON expenses (seq);
CREATE INDEX income_events_seq ON income_events (seq);
CREATE INDEX savings_allocations_seq ON savings_allocations (seq);
CREATE INDEX snapshots_seq ON snapshots (seq);

CREATE TRIGGER month_plans_seq BEFORE INSERT OR UPDATE ON month_plans FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER savings_goals_seq BEFORE INSERT OR UPDATE ON savings_goals FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER bill_templates_seq BEFORE INSERT OR UPDATE ON bill_templates FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER bills_seq BEFORE INSERT OR UPDATE ON bills FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER expenses_seq BEFORE INSERT OR UPDATE ON expenses FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER income_events_seq BEFORE INSERT OR UPDATE ON income_events FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER savings_allocations_seq BEFORE INSERT OR UPDATE ON savings_allocations FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER snapshots_seq BEFORE INSERT OR UPDATE ON snapshots FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
