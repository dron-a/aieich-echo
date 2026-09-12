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
    created_by      TEXT,
    last_updated_by TEXT,
    PRIMARY KEY (target_group, echo_title)
);

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
--
-- Renamed from current_affairs. Create this alongside the old view, point
-- the consumer at it, then drop current_affairs -- doing both at once
-- leaves a window where the old name is missing.
--
-- There is deliberately NO view over calendar: it would be the table with
-- a nicer name. Point anything that wants a calendar feed at the table.
CREATE OR REPLACE VIEW current_reminders AS
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

-- Small key/value store for the mail bot's cursor. Two rows:
--   mail_last_uid      highest IMAP UID processed in the label folder
--   mail_uid_validity  the folder's UIDVALIDITY when that UID was recorded.
-- If Gmail resets the folder, UIDVALIDITY changes and UIDs restart from 1;
-- comparing it is what stops a stale cursor from skipping every new mail.
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Calendar: everything the bot knows about, from every source. Append-mostly;
-- rows are never deleted on absence, because a source can stop returning an
-- event for reasons other than cancellation (lost access rights, rolled out
-- of a fetch window).
--
-- notice_kind is NOT stored -- it is a function of event_type:
--   assignment, quiz -> deadline (syncs to src_notice)
--   webinar          -> event    (syncs)
--   class            -> event    (never syncs)
--   announcement     -> none     (never syncs)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar (
    -- Stable across polls and across users:
    --   taxila  assign-<cmid> | quiz-<coursemodule>
    --   teams   ical-<uid>-<occurrence start>
    --   mail    mail-<uuid>
    event_key   TEXT PRIMARY KEY,
    source      TEXT NOT NULL,        -- taxila | teams | mail
    event_type  TEXT NOT NULL,        -- assignment | quiz | webinar | class | announcement
    subject_key TEXT NOT NULL,        -- AIMLZG521, or BITS_WILP
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,        -- composed in Python, sent as-is
    start_date  TIMESTAMPTZ NOT NULL, -- equals end_date for class/webinar/announcement
    end_date    TIMESTAMPTZ NOT NULL,
    echo_title  TEXT,                 -- set once synced to src_notice
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Candidate lookups and range queries ("what is due this week").
CREATE INDEX IF NOT EXISTS calendar_window_idx
    ON calendar (end_date, subject_key);

-- The sync step reads only unsynced future rows, so a partial index keeps it
-- flat however large the table grows.
CREATE INDEX IF NOT EXISTS calendar_unsynced_idx
    ON calendar (end_date) WHERE echo_title IS NULL;

-- Read-only feed for anything that wants upcoming items. Separate from
-- current_affairs, which another service already depends on.
-- CREATE OR REPLACE VIEW upcoming_events AS
-- SELECT event_key, source, event_type, subject_key, title, message,
--        start_date, end_date
-- FROM calendar
-- WHERE end_date >= now();

-- Periodic hygiene, run by hand. Announcements are notification-only and
-- have no value once they age out.
--   DELETE FROM calendar
--   WHERE event_type = 'announcement' AND end_date < now() - interval '90 days';

-- ---------------------------------------------------------------------------
-- Owned by the upstream bot, in its own Neon project. Read here over a second
-- connection (TAXILA_DATABASE_URL) once a day.
--
--   CREATE TABLE dim_taxila_usr (
--       user_id     TEXT PRIMARY KEY,
--       wstoken     TEXT NOT NULL,
--       teams_url   TEXT,
--       phone       TEXT,
--       label       TEXT,
--       is_active   BOOLEAN NOT NULL DEFAULT TRUE,
--       last_error  TEXT,
--       last_polled_at TIMESTAMPTZ
--   );
--
-- Ownership: the upstream bot writes user_id, wstoken, teams_url, phone and
-- label. The calendar bot writes last_polled_at and last_error after every
-- poll (see record_poll) -- insert them as NULL and never update them from
-- the registration flow.
--
-- is_active is manual and never flipped by the bot: a Taxila outage would
-- otherwise disable every user at once. Check for trouble with
--   SELECT user_id, last_polled_at, last_error FROM dim_taxila_usr
--   WHERE last_error IS NOT NULL;
-- ---------------------------------------------------------------------------

-- bot_state keys in use:
--   mail_last_uid       IMAP UID cursor
--   mail_uid_validity   folder UIDVALIDITY for that cursor
--   taxila_coverage     {"<user_id>": {"taxila": [...], "teams": [...]}}
--                       which subjects each user's sources yielded, so the
--                       greedy pass can skip users who add no coverage
--   taxila_last_poll    bucket key for the Taxila gate
--   teams_last_poll     bucket key for the Teams gate