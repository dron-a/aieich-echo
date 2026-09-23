-- echo notice bot schema (Postgres / Neon)
-- All timestamps are TIMESTAMPTZ; Postgres stores UTC internally and the
-- bot reads/writes in Asia/Kolkata.
-- A reminder's identity is (target_group, echo_title, created_by): two people
-- in one group may each hold a reminder with the same title.
--
-- Who may update what (enforced by echo_bot's fetch_pending join, and
-- expected at stage time upstream):
--   - the owner, on their own reminder
--   - anyone, on a calendar-synced reminder (created_by = 'echo_cal')
--   - an admin (ADMIN_PHONES env var), on anything

CREATE TABLE IF NOT EXISTS src_notice (
    target_group    TEXT        NOT NULL,
    echo_title      TEXT        NOT NULL,
    subject_key     TEXT        NOT NULL,
    message         TEXT        NOT NULL,
    start_date      TIMESTAMPTZ NOT NULL,
    end_date        TIMESTAMPTZ NOT NULL,
    schedule        JSONB       NOT NULL,
    notice_kind     TEXT,
    created_by      TEXT NOT NULL,
    last_updated_by TEXT,
    -- created_by is part of the identity, not just an attribute: two people
    -- in one group may each keep a reminder called mlops-1 for different
    -- things. Ownership is also authorisation -- the upstream bot filters
    -- update and remove on created_by, so nobody can change another
    -- person's reminder, and calendar-synced rows (created_by 'echo_cal')
    -- can be changed by nobody.
    PRIMARY KEY (target_group, echo_title, created_by)
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
    -- Matches src_notice. echo_bot's join requires created_by = updated_by,
    -- so an update staged by anyone other than the owner is never applied.
    PRIMARY KEY (target_group, echo_title, updated_by)
);

CREATE INDEX IF NOT EXISTS stg_notice_pending_idx
    ON stg_notice (target_group, echo_title, updated_by) WHERE processed = FALSE;

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
SELECT target_group, echo_title, created_by, message, schedule
FROM src_notice
WHERE end_date >= now();

-- Webhook dedup. The primary key is what makes dispatch exactly-once
-- across delayed, duplicated or overlapping scheduler runs.
CREATE TABLE IF NOT EXISTS sent_log (
    target_group TEXT     NOT NULL,
    echo_title   TEXT     NOT NULL,
    created_by   TEXT     NOT NULL,
    target_date  DATE     NOT NULL,
    target_hour  SMALLINT NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- created_by is in the key so that two same-titled reminders in one
    -- group claim their slot independently; without it one would suppress
    -- the other and only one person's reminder would ever send.
    PRIMARY KEY (target_group, echo_title, created_by, target_date, target_hour)
);

-- Upstream stage_update must reset processed on conflict, or a re-staged
-- update to an already-processed row is never picked up:
--
-- ON CONFLICT (target_group, echo_title, updated_by) DO UPDATE SET
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
--       phone       TEXT NOT NULL UNIQUE,
--       label       TEXT,
--       is_active   BOOLEAN NOT NULL DEFAULT TRUE,
--       last_error  TEXT,
--       last_polled_at TIMESTAMPTZ,
--       group_synced_at TIMESTAMPTZ,
--       course_synced_at TIMESTAMPTZ
--   );

-- CREATE UNIQUE INDEX ON dim_taxila_usr (phone) WHERE phone IS NOT NULL;
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
--   coverage_gaps       {"taxila": [...], "teams": [...]} -- subjects no
--                       working token could reach last run. Forces those
--                       users in on the next run instead of letting the
--                       greedy pass keep choosing the one that failed.

-- ---------------------------------------------------------------------------
-- Group membership. One row per (group, member), NOT per registered user:
-- two registered users in the same group would otherwise store the roster
-- twice. "Who is in my group" is one lookup on user_id.
--
-- user_id here is a Moodle user id and is a different population from
-- dim_taxila_usr.user_id -- every member is a Moodle user, only some have
-- registered with the bot. Do not assume a join between them resolves.
--
-- bits_id is derived from the email local part, not from the payload's
-- idnumber field: Moodle returns idnumber only for the account whose token
-- made the call, so reading it would fill one row per group and leave the
-- rest null.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_memberships (
    course_id   INT NOT NULL,
    group_id    INT NOT NULL,
    group_name  TEXT NOT NULL,
    user_id     INT NOT NULL,
    fullname    TEXT NOT NULL,
    bits_id     TEXT,
    email       TEXT,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS group_memberships_user_idx
    ON group_memberships (user_id);
CREATE INDEX IF NOT EXISTS group_memberships_course_idx
    ON group_memberships (course_id);

-- Owned by group_bot: stamped after a clean sync, NULL for a user who has
-- never been synced. Both the new-registration and the daily-refresh
-- triggers read this one column.
--
--   ALTER TABLE dim_taxila_usr ADD COLUMN group_synced_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- Course enrolment, per user. kind/subject_key/semester are derived from the
-- shortname: S1-26_AIMLZG521 -> subject, LW_DNN -> labware, anything else ->
-- resource. enddate is 0 in Moodle for labware and resources, stored NULL.
--
-- enddate > now() is how everything downstream picks "current semester" --
-- both S2-25 and S1-26 look like subjects by shortname, only the dates say
-- which is over.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_taxila_course (
    user_id      TEXT NOT NULL REFERENCES dim_taxila_usr(user_id) ON DELETE CASCADE,
    course_id    INTEGER NOT NULL,
    subject_key  TEXT,
    semester     TEXT,
    kind         TEXT NOT NULL,
    fullname     TEXT NOT NULL,
    shortname    TEXT NOT NULL,
    startdate    TIMESTAMPTZ,
    enddate      TIMESTAMPTZ,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, course_id)
);

CREATE INDEX IF NOT EXISTS dim_taxila_course_subject_idx
    ON dim_taxila_course (user_id, subject_key);
CREATE INDEX IF NOT EXISTS dim_taxila_course_current_idx
    ON dim_taxila_course (course_id, enddate);
-- ---------------------------------------------------------------------------
-- Course content: one row per module, whatever that module happens to be.
--
-- files and dates are loose on purpose. A resource has one PDF and no dates,
-- a folder has many files or none, a quiz has two dates and no files, a
-- forum has neither. Fixed columns would break the first time a course did
-- something unexpected, and the consumer is a model reading a whole course
-- at once rather than a query picking out artefacts.
--
-- dates is text, not columns: calendar already holds exact deadlines from a
-- verified source, and duplicating them here would give two places to
-- disagree.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_course_content (
    course_id    INTEGER NOT NULL,
    module_id    INTEGER NOT NULL,     -- cmid; same value calendar uses in event_key
    section_no   INTEGER,
    section_name TEXT,
    module_name  TEXT NOT NULL,
    modname      TEXT NOT NULL,        -- resource, folder, quiz, assign, forum, groupselect
    url          TEXT,
    files        JSONB,                -- [{"name":..,"url":..,"mime":..,"size":..}]
    dates        TEXT,                 -- "Opened: 30 Aug 2026 19:00; Closed: 06 Sep 2026 19:00"
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (course_id, module_id)
);

CREATE INDEX IF NOT EXISTS dim_course_content_course_idx
    ON dim_course_content (course_id);

-- Owned by course_bot, alongside group_synced_at:
--   ALTER TABLE dim_taxila_usr ADD COLUMN course_synced_at TIMESTAMPTZ;