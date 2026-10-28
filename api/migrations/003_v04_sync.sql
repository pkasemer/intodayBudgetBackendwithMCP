-- InToday backend v0.4 sync additions: new columns on three tables, five new tables.
-- Every new column is nullable or defaulted, so existing rows migrate untouched.
--
-- Deletes here are physical deletes (the retention purge, or a manual DELETE): synced deletes are
-- tombstones and never fire these rules. References the spec says "set null" or "store as sent"
-- are enforced as follows:
--   * real foreign keys where the parent always arrives first (plans, loans, goals);
--   * a delete trigger on debts, because bills.debt_id / debt_payments.debt_id must accept a debt
--     that arrives on a later page of a split push (so no FK can exist).

-- v0.4 §5.1: a goal with no target stores 0.
ALTER TABLE savings_goals DROP CONSTRAINT savings_goals_target_amount_check;
ALTER TABLE savings_goals ADD CONSTRAINT savings_goals_target_amount_check
    CHECK (target_amount BETWEEN 0 AND 9999999);

-- ---- new columns -----------------------------------------------------------------

ALTER TABLE month_plans
    ADD COLUMN closed_days            INTEGER[] NOT NULL DEFAULT '{}'
        CHECK (closed_days <@ '{1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31}'::integer[]),
    ADD COLUMN rollover_amount        INTEGER NOT NULL DEFAULT 0 CHECK (rollover_amount BETWEEN 0 AND 9999999),
    ADD COLUMN rollover_carried       BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN rollover_goal_id       UUID NULL REFERENCES savings_goals (id) ON DELETE SET NULL,
    ADD COLUMN summary_shown          BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN buffer_released_amount INTEGER NOT NULL DEFAULT 0 CHECK (buffer_released_amount BETWEEN 0 AND 9999999),
    ADD COLUMN buffer_released_on     DATE NULL,
    ADD COLUMN reserved_debt_ids      UUID[] NOT NULL DEFAULT '{}',  -- ids only; debts may be gone, no FK
    ADD COLUMN created_at             TIMESTAMPTZ NULL;
CREATE INDEX month_plans_rollover_goal ON month_plans (rollover_goal_id) WHERE rollover_goal_id IS NOT NULL;

ALTER TABLE bills
    ADD COLUMN is_included    BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN exclude_reason TEXT NULL CHECK (length(exclude_reason) <= 50),
    ADD COLUMN debt_id        UUID NULL;  -- no FK: see header
CREATE INDEX bills_debt_id ON bills (debt_id) WHERE debt_id IS NOT NULL;

ALTER TABLE income_events
    ADD COLUMN is_recurring BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN is_rollover  BOOLEAN NOT NULL DEFAULT false;

-- ---- new tables ------------------------------------------------------------------

CREATE TABLE debts (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL CHECK (length(btrim(name)) > 0 AND length(name) <= 100),
    kind            TEXT NOT NULL CHECK (kind IN ('person', 'loan')),
    amount          INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 999999999),
    started_on      DATE NOT NULL,
    monthly_payment INTEGER NOT NULL DEFAULT 0 CHECK (monthly_payment BETWEEN 0 AND 999999999),
    due_day         INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    first_month     TEXT NOT NULL CHECK (first_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    updated_at      TIMESTAMPTZ NOT NULL,
    device_id       TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NULL,
    seq             BIGINT NOT NULL DEFAULT 0,
    CHECK (monthly_payment <= amount)
);

CREATE TABLE loans (
    id          UUID PRIMARY KEY,
    plan_id     UUID NULL REFERENCES month_plans (id) ON DELETE SET NULL,
    person      TEXT NOT NULL CHECK (length(btrim(person)) > 0 AND length(person) <= 100),
    amount      INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    lent_on     DATE NOT NULL,
    due_on      DATE NULL,
    repaid_on   DATE NULL,
    written_off BOOLEAN NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL,
    device_id   TEXT NOT NULL,
    deleted_at  TIMESTAMPTZ NULL,
    seq         BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX loans_plan_id ON loans (plan_id) WHERE plan_id IS NOT NULL;
-- "still owed" is the cheap query: repaid_on IS NULL AND NOT written_off
CREATE INDEX loans_open ON loans (due_on) WHERE repaid_on IS NULL AND NOT written_off AND deleted_at IS NULL;

CREATE TABLE loan_repayments (
    id               UUID PRIMARY KEY,
    loan_id          UUID NOT NULL REFERENCES loans (id) ON DELETE CASCADE,
    amount           INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    date             DATE NOT NULL,
    landed_as_income BOOLEAN NOT NULL DEFAULT false,
    updated_at       TIMESTAMPTZ NOT NULL,
    device_id        TEXT NOT NULL,
    deleted_at       TIMESTAMPTZ NULL,
    seq              BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX loan_repayments_loan_id ON loan_repayments (loan_id);

CREATE TABLE debt_payments (
    id         UUID PRIMARY KEY,
    debt_id    UUID NULL,  -- no FK: see header
    plan_id    UUID NULL REFERENCES month_plans (id) ON DELETE CASCADE,
    amount     INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    date       DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    device_id  TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NULL,
    seq        BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX debt_payments_debt_id ON debt_payments (debt_id) WHERE debt_id IS NOT NULL;
CREATE INDEX debt_payments_plan_id ON debt_payments (plan_id) WHERE plan_id IS NOT NULL;

CREATE TABLE category_limits (
    id         UUID PRIMARY KEY,
    tag        TEXT NOT NULL CHECK (tag IN ('food', 'transport', 'dataAirtime', 'fun', 'shopping', 'other')),
    amount     INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 9999999),
    updated_at TIMESTAMPTZ NOT NULL,
    device_id  TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NULL,
    seq        BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX category_limits_tag_live ON category_limits (tag) WHERE deleted_at IS NULL;

-- Deleting a debt detaches the rows that name it (they stay, with debt_id null). The updates
-- fire the seq trigger, so devices pull the detached rows.
CREATE FUNCTION debts_release_refs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE bills SET debt_id = NULL WHERE debt_id = OLD.id;
    UPDATE debt_payments SET debt_id = NULL WHERE debt_id = OLD.id;
    RETURN OLD;
END
$$;
CREATE TRIGGER debts_release_refs BEFORE DELETE ON debts FOR EACH ROW EXECUTE FUNCTION debts_release_refs();

-- Sync bookkeeping, as for every other table.
CREATE INDEX debts_updated_at ON debts (updated_at);
CREATE INDEX loans_updated_at ON loans (updated_at);
CREATE INDEX loan_repayments_updated_at ON loan_repayments (updated_at);
CREATE INDEX debt_payments_updated_at ON debt_payments (updated_at);
CREATE INDEX category_limits_updated_at ON category_limits (updated_at);

CREATE INDEX debts_seq ON debts (seq);
CREATE INDEX loans_seq ON loans (seq);
CREATE INDEX loan_repayments_seq ON loan_repayments (seq);
CREATE INDEX debt_payments_seq ON debt_payments (seq);
CREATE INDEX category_limits_seq ON category_limits (seq);

CREATE TRIGGER debts_seq BEFORE INSERT OR UPDATE ON debts FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER loans_seq BEFORE INSERT OR UPDATE ON loans FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER loan_repayments_seq BEFORE INSERT OR UPDATE ON loan_repayments FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER debt_payments_seq BEFORE INSERT OR UPDATE ON debt_payments FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
CREATE TRIGGER category_limits_seq BEFORE INSERT OR UPDATE ON category_limits FOR EACH ROW EXECUTE FUNCTION set_sync_seq();
