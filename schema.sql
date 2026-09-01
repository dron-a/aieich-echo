-- echo notice bot schema (Postgres / Neon)
-- All timestamps are TIMESTAMPTZ; Postgres stores UTC internally and the
-- bot reads/writes in Asia/Kolkata.
-- Identity is (target_group, echo_title) throughout: a title is unique
-- within a group, not globally.

CREATE TABLE IF NOT EXISTS src_notice (
    target_group    TEXT        NOT NULL,
    echo_title      TEXT        NOT NULL,
    subject_key     TEXT        NOT NULL,
    message         TEXT        NOT NULL,
    start_date      TIMESTAMPTZ NOT NULL,
    end_date        TIMESTAMPTZ NOT NULL,
    schedule        JSONB       NOT NULL,
    notice_kind     TEXT,
    created_by      TEXT,
    last_updated_by TEXT,
    PRIMARY KEY (target_group, echo_title)
);

-- notice_kind is "deadline", "event" or NULL, classified by the upstream
-- bot at set time. Immutable: the update path must never change it, so it
-- is absent from the bot's mutable-field set. NULL is treated as unknown.
--
-- target_group is set by upstream on every "set", never shown to the user
-- and never mutable. The bot reads it to key rows and to build the webhook
-- payload, so it is deliberately absent from anything the LLM sees.
CREATE INDEX IF NOT EXISTS src_notice_window_idx
    ON src_notice (end_date, start_date);

-- Staging queue. Upstream writes update-intents here; every row is an
-- update (set/remove go straight to src_notice). processed defaults FALSE
-- so upstream never has to mention the column on insert.
CREATE TABLE IF NOT EXISTS stg_notice (
    target_group   TEXT    NOT NULL,
    echo_title     TEXT    NOT NULL,
    action_type    TEXT,
    comments       TEXT,
    update_message TEXT    NOT NULL,
    updated_by     TEXT,
    processed      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (target_group, echo_title)
);

CREATE INDEX IF NOT EXISTS stg_notice_pending_idx
    ON stg_notice (target_group, echo_title) WHERE processed = FALSE;

-- Read by the downstream service. A view: no storage, no hourly refresh,
-- no truncate window for readers to land in. target_group is included
-- because echo_title alone is no longer unique.
CREATE OR REPLACE VIEW current_affairs AS
SELECT target_group, echo_title, message, schedule
FROM src_notice
WHERE end_date >= now();

-- Webhook dedup. The primary key is what makes dispatch exactly-once
-- across delayed, duplicated or overlapping scheduler runs.
CREATE TABLE IF NOT EXISTS sent_log (
    target_group TEXT     NOT NULL,
    echo_title   TEXT     NOT NULL,
    target_date  DATE     NOT NULL,
    target_hour  SMALLINT NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_group, echo_title, target_date, target_hour)
);

-- Upstream stage_update must reset processed on conflict, or a re-staged
-- update to an already-processed row is never picked up:
--
-- ON CONFLICT (target_group, echo_title) DO UPDATE SET
--     action_type    = EXCLUDED.action_type,
--     comments       = EXCLUDED.comments,
--     update_message = EXCLUDED.update_message,
--     updated_by     = EXCLUDED.updated_by,
--     processed      = FALSE;