#!/usr/bin/env python3
"""
Calendar bot. Runs from run.py, once a day, after echo_bot and mail_bot.

  poll_taxila   assignment and quiz deadlines, per registered user
  poll_teams    classes and webinars, from each user's published ICS
  sync_notices  turn calendar rows into reminders in src_notice

Everything here is deterministic -- no LLM. Taxila returns structured dates
and stable module ids; Teams returns an ICS. Both give exact values, so
nothing needs inferring.

calendar is append-mostly. A row is never deleted because a source stopped
returning it: Taxila hides modules the token cannot read (the response
carries a warnings block saying so), and events roll out of the fetch
window. Absence is not cancellation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta

import httpx
import psycopg
from psycopg.rows import dict_row

import ics
from echo_bot import IST, connect
from app_context import SUBJECT_NAMES

log = logging.getLogger("cal_bot")

TAXILA_DATABASE_URL = os.environ.get("TAXILA_DATABASE_URL", "")
TAXILA_API_URL = os.environ.get(
    "TAXILA_API_URL", "https://taxila-aws.bits-pilani.ac.in/webservice/rest/server.php"
)

CAL_ENABLED = os.environ.get("CAL_ENABLED", "1") not in ("0", "false", "")
CAL_BACK_DAYS = int(os.environ.get("CAL_BACK_DAYS", "30"))
CAL_FWD_DAYS = int(os.environ.get("CAL_FWD_DAYS", "60"))
CAL_TIMEOUT_S = float(os.environ.get("CAL_TIMEOUT_S", "30"))

# Hours between polls, per source. Taxila deadlines move and get extended,
# so it is checked often; Teams timetables change rarely, so once a day is
# enough to catch a rescheduled webinar long before its reminders start.
#
# Bucketed rather than elapsed-time: a run at 14:00:01 compared against a
# poll at 10:00:02 is four seconds short of four hours, so an elapsed check
# would skip it and every cycle would drift an hour later. Flooring the
# hour into a bucket keeps polls on 00/04/08/12/16/20. 24 means once a day;
# 1 means every run.
TAXILA_POLL_INTERVAL_H = max(1, min(24, int(
    os.environ.get("TAXILA_POLL_INTERVAL_H", "4"))))
TEAMS_POLL_INTERVAL_H = max(1, min(24, int(
    os.environ.get("TEAMS_POLL_INTERVAL_H", "24"))))


def poll_bucket(now: datetime, interval_h: int) -> str:
    """Stable key for the interval this moment falls in."""
    return "%s-%02d" % (
        now.date().isoformat(), (now.hour // interval_h) * interval_h
    )
CAL_PAGE_SIZE = int(os.environ.get("CAL_PAGE_SIZE", "50"))
CAL_MAX_PAGES = int(os.environ.get("CAL_MAX_PAGES", "6"))

# Turn calendar rows into reminders. On by default -- a kill switch, not a
# trial gate. Capped so that enabling it after a long pause drains over a
# few hours instead of firing everything at once.
NOTICE_SYNC = os.environ.get("CALENDAR_NOTICE_SYNC", "1") not in ("0", "false", "")
SYNC_BATCH = int(os.environ.get("CALENDAR_SYNC_BATCH", "20"))
NOTICE_GROUP = os.environ.get("MAIL_TARGET_GROUP", "")

# Days before end_date on which an automated reminder fires. The list is
# self-filtering: a deadline four days out simply never reaches the 21- and
# 14-day entries, so the number of reminders scales with how much notice
# there was, without any per-item arithmetic.
#
# Keep the furthest entry under about 28. month_days holds day-of-month
# numbers, so a milestone more than a month back would collide with the
# deadline's own month.
def _parse_days_out(raw: str) -> tuple[int, ...]:
    try:
        days = sorted({int(d) for d in raw.split(",") if d.strip() != ""}, reverse=True)
        if not days or days[0] > 28 or days[-1] < 0:
            raise ValueError(raw)
        return tuple(days)
    except Exception:
        log.warning("SYNC_DAYS_OUT %r is malformed, using defaults", raw)
        return (21, 14, 9, 7, 5, 3, 1, 0)


SYNC_DAYS_OUT = _parse_days_out(
    os.environ.get("SYNC_DAYS_OUT", "21,14,9,7,5,3,1,0")
)

CODE_RE = re.compile(r"\b((?:S\d-\d{2})_)?(AIML[A-Z]*\d{3})\b")

# event_type -> (notice_kind, hour). None means the type never syncs.
SYNCABLE = {
    "assignment": ("deadline", 10),
    "quiz": ("deadline", 10),
    "webinar": ("event", 9),
}


def course_name(subject_key: str) -> str:
    return SUBJECT_NAMES.get(subject_key, subject_key)


def fmt(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%d %b %H:%M")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def load_users() -> list[dict]:
    """Registered users, from the upstream bot's own Neon project.

    A second connection because Neon projects are separate databases with no
    cross-database queries. Opened once a day and closed immediately.
    """
    if not TAXILA_DATABASE_URL:
        return []
    with psycopg.connect(TAXILA_DATABASE_URL, connect_timeout=30) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT user_id, wstoken, teams_url FROM dim_taxila_usr "
                "WHERE COALESCE(is_active, TRUE)"
            )
            return cur.fetchall()


def record_poll(results: list[tuple[str, str | None]]) -> None:
    """Write poll outcomes back to dim_taxila_usr.

    A dead token is the failure mode that hides: that user's courses simply
    stop being covered, the greedy selection keeps skipping everyone else
    for those subjects, and nothing surfaces it. last_error makes it a
    query instead of a complaint.

    is_active is deliberately NOT flipped on failure -- a Taxila outage
    would disable every user at once. The column stays manual.

    Best effort: the upstream database is a separate Neon project, and
    failing to write a diagnostic must not fail the poll that produced it.
    """
    if not TAXILA_DATABASE_URL or not results:
        return

    try:
        with psycopg.connect(TAXILA_DATABASE_URL, connect_timeout=30) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    UPDATE dim_taxila_usr
                    SET last_polled_at = now(), last_error = %s
                    WHERE user_id = %s
                    """,
                    [(err, uid) for uid, err in results],
                )
        failed = sum(1 for _, err in results if err)
        if failed:
            log.warning("%d of %d users failed to poll", failed, len(results))
    except Exception as exc:
        log.warning("could not record poll status: %s", exc)


def select_users(users: list[dict], coverage: dict[str, list[str]]) -> list[dict]:
    """Smallest set of users covering every known subject.

    Greedy set cover over the cached {user_id: [subject_key]} map. Two
    students on the same course produce identical rows, so polling both is
    wasted work.

    Users absent from the cache are always polled -- that is what picks up a
    new registration, with no invalidation step. A cached user who is not
    selected may drift out of date, which is harmless: the cache only decides
    who to skip, and the greedy pass never skips the last user covering a
    subject.
    """
    def subjects_of(uid: str) -> set[str]:
        """Flatten a user's per-source coverage. Tolerates the old flat
        list shape from before Taxila and Teams were gated separately."""
        entry = coverage.get(uid) or {}
        if isinstance(entry, list):
            return set(entry)
        return {s for subjects in entry.values() for s in subjects}

    unknown = [u for u in users if str(u["user_id"]) not in coverage]
    known = [u for u in users if str(u["user_id"]) in coverage]

    remaining = {s for u in known for s in subjects_of(str(u["user_id"]))}
    chosen = list(unknown)

    while remaining:
        best, gain = None, 0
        for u in known:
            if u in chosen:
                continue
            n = len(remaining & subjects_of(str(u["user_id"])))
            if n > gain:
                best, gain = u, n
        if not best:
            break
        chosen.append(best)
        remaining -= subjects_of(str(best["user_id"]))

    if len(chosen) < len(users):
        log.info("polling %d of %d users (coverage cache)", len(chosen), len(users))
    return chosen


# ---------------------------------------------------------------------------
# Taxila
# ---------------------------------------------------------------------------


def _ws(client: httpx.Client, token: str, fn: str, **params) -> dict | list:
    r = client.get(
        TAXILA_API_URL,
        params={
            "wstoken": token,
            "wsfunction": fn,
            "moodlewsrestformat": "json",
            **params,
        },
    )
    r.raise_for_status()
    data = r.json()
    # Moodle answers 200 with an exception body rather than an HTTP error.
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError("%s: %s" % (fn, data.get("errorcode", "unknown")))
    return data


def _discover(client: httpx.Client, token: str, start: datetime, end: datetime) -> list[dict]:
    """Calendar events in the window, paged.

    limitnum caps at 50 and Moodle truncates silently, so a full page means
    there may be more: continue from the last event's timesort. Dedup is by
    event_key downstream, so an overlapping boundary event costs nothing.
    """
    events, cursor = [], int(start.timestamp())
    for _ in range(CAL_MAX_PAGES):
        batch = _ws(
            client,
            token,
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=cursor,
            timesortto=int(end.timestamp()),
            limitnum=CAL_PAGE_SIZE,
        ).get("events", [])
        events += batch
        if len(batch) < CAL_PAGE_SIZE:
            break
        cursor = batch[-1]["timesort"] + 1
    return events


def poll_taxila_user(client: httpx.Client, token: str, start: datetime, end: datetime):
    """(rows, subject_keys) for one user.

    Discovery first, then enrich only the module types present. The calendar
    endpoint gives the deadline but not the open date; mod_assign and mod_quiz
    give both, so they are called only when the window actually contains an
    assignment or a quiz.
    """
    events = _discover(client, token, start, end)
    if not events:
        return [], set()

    course_key: dict[int, str] = {}
    for e in events:
        c = e.get("course") or {}
        m = CODE_RE.search(c.get("shortname") or "")
        if c.get("id"):
            course_key[c["id"]] = m.group(2).upper() if m else "BITS_WILP"

    def ids_for(module: str) -> list[int]:
        return sorted(
            {
                (e.get("course") or {}).get("id")
                for e in events
                if e.get("modulename") == module and (e.get("course") or {}).get("id")
            }
        )

    # cmid / coursemodule -> the module's own dates. Keyed on the same value
    # the calendar calls "instance", which is what makes event_key stable.
    assign_dates: dict[int, tuple[int, int]] = {}
    quiz_dates: dict[int, tuple[int, int, int]] = {}

    if course_ids := ids_for("assign"):
        try:
            data = _ws(
                client,
                token,
                "mod_assign_get_assignments",
                **{f"courseids[{i}]": cid for i, cid in enumerate(course_ids)},
            )
            for course in data.get("courses", []):
                for a in course.get("assignments", []):
                    # cutoffdate, when set, is the moment submission is
                    # actually blocked; duedate only marks late.
                    end_ts = a.get("cutoffdate") or a.get("duedate") or 0
                    assign_dates[a["cmid"]] = (a.get("allowsubmissionsfromdate") or 0, end_ts)
        except Exception as exc:
            log.warning("assign enrich failed: %s", exc)

    if course_ids := ids_for("quiz"):
        try:
            data = _ws(
                client,
                token,
                "mod_quiz_get_quizzes_by_courses",
                **{f"courseids[{i}]": cid for i, cid in enumerate(course_ids)},
            )
            for q in data.get("quizzes", []):
                quiz_dates[q["coursemodule"]] = (
                    q.get("timeopen") or 0,
                    q.get("timeclose") or 0,
                    q.get("timelimit") or 0,
                )
        except Exception as exc:
            log.warning("quiz enrich failed: %s", exc)

    rows, subjects = [], set()
    for e in events:
        module, instance = e.get("modulename"), e.get("instance")
        if not module or not instance:
            continue

        cid = (e.get("course") or {}).get("id")
        key = course_key.get(cid, "BITS_WILP")
        subjects.add(key)
        name = course_name(key)
        title = e.get("activityname") or e.get("name") or "Untitled"
        deadline = datetime.fromtimestamp(e["timesort"], IST)

        if module == "assign" and instance in assign_dates:
            opens, closes = assign_dates[instance]
            end_dt = datetime.fromtimestamp(closes, IST) if closes else deadline
            start_dt = datetime.fromtimestamp(opens, IST) if opens else end_dt
            message = "%s (%s) due %s." % (title, name, fmt(end_dt))
            if opens:
                message += " Open from %s." % fmt(start_dt)
            event_type = "assignment"

        elif module == "quiz" and instance in quiz_dates:
            opens, closes, limit = quiz_dates[instance]
            end_dt = datetime.fromtimestamp(closes, IST) if closes else deadline
            start_dt = datetime.fromtimestamp(opens, IST) if opens else end_dt
            message = "%s (%s) closes %s." % (title, name, fmt(end_dt))
            extra = []
            if opens:
                extra.append("opens %s" % fmt(start_dt))
            if limit:
                extra.append("%d min limit" % (limit // 60))
            if extra:
                joined = ", ".join(extra)
                message += " " + joined[0].upper() + joined[1:] + "."
            event_type = "quiz"

        else:
            # Third-party modules (groupselect and friends) have no bulk
            # endpoint. The calendar's own timesort is all there is.
            start_dt = end_dt = deadline
            message = "%s (%s) due %s." % (title, name, fmt(end_dt))
            event_type = "assignment" if e.get("eventtype") == "due" else "quiz"

        rows.append(
            {
                "event_key": "%s-%s" % (module, instance),
                "source": "taxila",
                "event_type": event_type,
                "subject_key": key,
                "title": title,
                "message": message,
                "start_date": start_dt,
                "end_date": end_dt,
            }
        )

    return rows, subjects


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def poll_teams_user(client: httpx.Client, url: str, start: datetime, end: datetime):
    """Classes and webinars from one published ICS.

    Each URL is a personal calendar, so there is nothing to share between
    users before fetching -- but the same class in two calendars carries the
    same UID, so event_key collapses them on write.
    """
    r = client.get(url)
    r.raise_for_status()

    rows, subjects = [], set()
    for occ in ics.parse_calendar(r.text, start, end):
        key = occ["subject_key"]
        subjects.add(key)
        name = course_name(key)
        label = "Webinar" if occ["event_type"] == "webinar" else "Class"
        rows.append(
            {
                # Lowercased: event_key doubles as echo_title once a
                # webinar syncs, and the upstream bot lowercases echo_title
                # on the way in. The UID slice is hex, so case carries no
                # information and folding it cannot collide.
                # Lowercased whole: event_key doubles as echo_title once a
                # webinar syncs, and the upstream bot lowercases echo_title
                # on the way in. The UID slice is hex and the timestamp's
                # only letter is the ISO "T", so folding case loses nothing
                # and cannot collide.
                "event_key": ("ical-%s-%s"
                              % (occ["uid"][-16:], occ["start"].isoformat())).lower(),
                "source": "teams",
                "event_type": occ["event_type"],
                "subject_key": key,
                "title": "%s %s" % (name, label.lower()),
                "message": "%s %s on %s to %s."
                % (name, label.lower(), fmt(occ["start"]), fmt(occ["end"])[-5:]),
                "start_date": occ["start"],
                "end_date": occ["end"],
            }
        )
    return rows, subjects


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def upsert(conn: psycopg.Connection, rows: list[dict]) -> int:
    """Write calendar rows. echo_title is never touched -- once a row has
    produced a reminder, that link survives every later poll."""
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO calendar (event_key, source, event_type, subject_key,
                                  title, message, start_date, end_date)
            VALUES (%(event_key)s, %(source)s, %(event_type)s, %(subject_key)s,
                    %(title)s, %(message)s, %(start_date)s, %(end_date)s)
            ON CONFLICT (event_key) DO UPDATE SET
                event_type  = EXCLUDED.event_type,
                subject_key = EXCLUDED.subject_key,
                title       = EXCLUDED.title,
                message     = EXCLUDED.message,
                start_date  = EXCLUDED.start_date,
                end_date    = EXCLUDED.end_date,
                updated_at  = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def read_state(conn: psycopg.Connection, key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def write_state(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync to src_notice
# ---------------------------------------------------------------------------


def derive_schedule(hour: int, end: datetime, start: datetime | None = None) -> dict:
    """Fire at each milestone before the deadline, plus the day itself.

    Counted back from end_date and expressed as month_days, not
    day_interval: a true start_date can be weeks in the past, and an
    interval counted from it would mean reminders from the day the
    assignment opened. Naming the days gives a fixed cadence however early
    the item was announced.

    Milestones already past simply never fire -- start_date gates them --
    so an item learned about three days before its deadline quietly gets
    the last two entries.

    month_days recurs monthly in principle, but end_date prunes the notice
    long before it could come round again.
    """
    end_ist = end.astimezone(IST)
    floor = (start or end).astimezone(IST)
    # Milestones before the item existed are dropped rather than left to be
    # gated by start_date: month_days entries recur, so a stale day could
    # fire if the deadline were later extended into the following month.
    days = {
        (end_ist - timedelta(days=d)).day
        for d in SYNC_DAYS_OUT
        if (end_ist - timedelta(days=d)).date() >= floor.date()
    }
    if not days:
        days = {end_ist.day}
    return {"hours": [hour], "month_days": sorted(days)}


def sync_notices(conn: psycopg.Connection) -> int:
    """Create and update reminders from calendar rows.

    One query covers both: a LEFT JOIN finds rows that have never been
    synced (no matching notice) and rows whose notice has drifted from the
    calendar (a rescheduled quiz, a moved webinar). Both go through the same
    write, so the schedule rule lives in exactly one place and a changed
    end_date recomputes its milestones instead of leaving them stale.

    Announcements and classes never appear here. Announcements are written
    with end_date set to the moment they arrived, so they fail "end_date >
    now()" by the next run, and neither type is in SYNCABLE -- two
    independent reasons, because a notification-only item turning into a
    reminder would be a visible bug.

    A notice a human has touched is left alone: created_by proves it began
    here, last_updated_by proves nothing has edited it since. Any human
    write flips last_updated_by and it stays flipped, because this is the
    only thing that would set it back and it has just excluded itself.
    """
    if not NOTICE_SYNC or not NOTICE_GROUP:
        return 0

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT c.event_key, c.event_type, c.subject_key, c.message,
                   c.start_date, c.end_date, c.echo_title IS NULL AS is_new
            FROM calendar c
            LEFT JOIN src_notice s
                   ON s.echo_title = c.event_key
                  AND s.target_group = %(grp)s
            WHERE c.end_date > now()
              AND c.event_type = ANY(%(types)s)
              AND (
                    c.echo_title IS NULL
                 OR (s.echo_title IS NOT NULL
                     AND s.created_by = 'echo_cal'
                     AND s.last_updated_by = 'echo_cal'
                     AND (s.message, s.start_date, s.end_date, s.subject_key)
                         IS DISTINCT FROM
                         (c.message, c.start_date, c.end_date, c.subject_key))
                  )
            ORDER BY c.end_date
            LIMIT %(batch)s
            """,
            {"grp": NOTICE_GROUP, "types": list(SYNCABLE), "batch": SYNC_BATCH},
        )
        pending = cur.fetchall()
    conn.commit()

    if not pending:
        return 0

    created = updated = 0
    with conn.cursor() as cur:
        for row in pending:
            kind, hour = SYNCABLE[row["event_type"]]
            schedule = derive_schedule(hour, row["end_date"], row["start_date"])

            cur.execute(
                """
                INSERT INTO src_notice (target_group, echo_title, subject_key,
                                        message, start_date, end_date, schedule,
                                        notice_kind, created_by, last_updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'echo_cal', 'echo_cal')
                ON CONFLICT (target_group, echo_title) DO UPDATE SET
                    subject_key = EXCLUDED.subject_key,
                    message     = EXCLUDED.message,
                    start_date  = EXCLUDED.start_date,
                    end_date    = EXCLUDED.end_date,
                    schedule    = EXCLUDED.schedule,
                    notice_kind = EXCLUDED.notice_kind,
                    last_updated_by = 'echo_cal'
                """,
                (
                    NOTICE_GROUP,
                    row["event_key"],
                    row["subject_key"],
                    row["message"],
                    row["start_date"],
                    row["end_date"],
                    json.dumps(schedule),
                    kind,
                ),
            )

            if row["is_new"]:
                # echo_title on the calendar row is what makes "already
                # synced" a local fact, so the partial index can serve it.
                cur.execute(
                    "UPDATE calendar SET echo_title = %s WHERE event_key = %s",
                    (row["event_key"], row["event_key"]),
                )
                created += 1
            else:
                updated += 1
    conn.commit()

    if created or updated:
        log.info("synced %d new, %d changed", created, updated)
    return created + updated

    # log.info("synced %d calendar rows to src_notice", created)
    # return created


# def resync_changed(conn: psycopg.Connection) -> int:
#     """Push date and message changes to reminders already created.

#     calendar is authoritative: a rescheduled quiz or a moved webinar updates
#     here, and the reminder follows. One statement, no row-by-row work.
#     """
#     if not NOTICE_SYNC:
#         return 0

#     with conn.cursor() as cur:
#         cur.execute(
#             """
#             UPDATE src_notice s
#             SET message = c.message, start_date = c.start_date,
#                 end_date = c.end_date, last_updated_by = 'echo_cal'
#             FROM calendar c
#             WHERE s.echo_title = c.echo_title
#               AND s.created_by = 'echo_cal'
#               AND c.end_date > now()
#               AND (s.message, s.start_date, s.end_date)
#                   IS DISTINCT FROM (c.message, c.start_date, c.end_date)
#             """
#         )
#         changed = cur.rowcount
#     conn.commit()
#     if changed:
#         log.info("updated %d reminders from calendar changes", changed)
#     return changed


# ---------------------------------------------------------------------------


def main() -> int:
    if not CAL_ENABLED:
        return 0

    now = datetime.now(IST)
    taxila_bucket = poll_bucket(now, TAXILA_POLL_INTERVAL_H)
    teams_bucket = poll_bucket(now, TEAMS_POLL_INTERVAL_H)

    conn = connect()
    try:
        do_taxila = read_state(conn, "taxila_last_poll") != taxila_bucket
        do_teams = read_state(conn, "teams_last_poll") != teams_bucket

        if not (do_taxila or do_teams):
            # Nothing due this hour. The sync step still runs -- it is one
            # indexed lookup, and it is what picks up a flipped kill switch
            # or a row that failed to sync earlier.
            sync_notices(conn)
            # resync_changed(conn)
            return 0

        coverage_raw = read_state(conn, "taxila_coverage")
        coverage = json.loads(coverage_raw) if coverage_raw else {}
    finally:
        conn.close()

    try:
        users = load_users()
    except Exception:
        log.exception("could not load users")
        users = []

    if not users:
        log.info("no registered users")
        return 0

    window_start = now - timedelta(days=CAL_BACK_DAYS)
    window_end = now + timedelta(days=CAL_FWD_DAYS)

    rows: list[dict] = []
    seen: dict[str, list[str]] = {}

    # All network work happens with no database connection held.
    outcomes: list[tuple[str, str | None]] = []

    with httpx.Client(timeout=CAL_TIMEOUT_S, follow_redirects=True) as client:
        for user in select_users(users, coverage):
            uid = str(user["user_id"])
            errors: list[str] = []
            # Tracked per source: a Taxila-only run must not wipe the
            # subjects a user's Teams calendar contributed, or the greedy
            # selection would think those courses are uncovered.
            found: dict[str, set[str]] = {}

            if do_taxila and user.get("wstoken"):
                try:
                    r, s = poll_taxila_user(
                        client, user["wstoken"], window_start, window_end
                    )
                    rows += r
                    found["taxila"] = s
                except Exception as exc:
                    # Truncated: the column is a diagnostic, and a Moodle
                    # exception body can be long. Never log the token.
                    errors.append("taxila: %s" % str(exc)[:200])
                    log.warning("taxila poll failed for %s: %s", uid, exc)

            if do_teams and user.get("teams_url"):
                try:
                    r, s = poll_teams_user(
                        client, user["teams_url"], window_start, window_end
                    )
                    rows += r
                    found["teams"] = s
                except Exception as exc:
                    errors.append("teams: %s" % str(exc)[:200])
                    log.warning("teams poll failed for %s: %s", uid, exc)

            outcomes.append((uid, "; ".join(errors) if errors else None))
            if found:
                seen[uid] = found

    record_poll(outcomes)

    # Same event from two users collapses here, before it reaches the database.
    unique = {r["event_key"]: r for r in rows}
    log.info("%d events (%d after dedup)", len(rows), len(unique))

    conn = connect()
    try:
        written = upsert(conn, list(unique.values()))

        # Merge per source. A Taxila-only run carries no Teams subjects,
        # and overwriting the user's entry wholesale would drop them --
        # the greedy pass would then think those courses are uncovered and
        # poll everyone again.
        for uid, found in seen.items():
            entry = coverage.setdefault(uid, {})
            if isinstance(entry, list):
                # Upgrade the old flat shape written by earlier versions.
                entry = {"taxila": entry}
                coverage[uid] = entry
            for source, subjects in found.items():
                entry[source] = sorted(subjects)

        write_state(conn, "taxila_coverage", json.dumps(coverage))
        if do_taxila:
            write_state(conn, "taxila_last_poll", taxila_bucket)
        if do_teams:
            write_state(conn, "teams_last_poll", teams_bucket)
        log.info("calendar rows written: %d", written)

        sync_notices(conn)
        # resync_changed(conn)
    finally:
        conn.close()

    return 0