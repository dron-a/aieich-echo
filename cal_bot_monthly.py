#!/usr/bin/env python3
"""
Calendar bot -- monthly-view variant. Drop-in replacement for cal_bot.py:
same signatures, same row shape, same tables. Switch by changing the import
in run.py.

Why this exists. cal_bot.py discovers events through
core_calendar_get_action_events_by_timesort, which is the dashboard
timeline: an ACTION list, not a calendar. Once a student actions an item it
stops being returned for that user. For a group bot polling one person's
token, one student submitting early makes a deadline invisible to everyone,
silently. The same design then enriches via mod_assign_get_assignments,
which omits modules the token cannot read and reports them only in a
warnings block.

core_calendar_get_calendar_monthly_view has neither problem. It is the
calendar proper: not action-filtered, no limitnum truncation, no
per-module capability gaps. Quizzes appear as two events -- one "open", one
"close" -- sharing an instance, so pairing them yields both exact times and
the module endpoints are not needed at all.

  poll_taxila   assignment and quiz deadlines, per registered user
  poll_teams    classes and webinars, from each user's published ICS
  sync_notices  turn calendar rows into reminders in src_notice

Everything here is deterministic -- no LLM.

calendar is append-mostly. A row is never deleted because a source stopped
returning it: events roll out of the fetch window, and absence is not
cancellation.
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
# Months fetched per poll: this one and the next. No back-poll -- every
# event carries its own partner boundary, so September's "Quiz 1 closes"
# tells us the quiz opened on 30 August without fetching August.
CAL_MONTHS = int(os.environ.get("CAL_MONTHS", "2"))

# One extra call per poll to fetch quiz time limits and attempt counts,
# which the monthly view does not carry. Purely cosmetic: the deadline
# never depends on it, and mod_quiz_get_quizzes_by_courses omits modules
# the token cannot read (reporting them in a warnings block), so a quiz
# that fails to enrich simply loses the detail. Set 0 to stop the call.
QUIZ_ENRICH = os.environ.get("QUIZ_ENRICH", "1") not in ("0", "false", "")

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


def long_fmt(dt: datetime) -> str:
    """"Thursday 3 Sep, 11:59 PM" -- weekday first so nobody has to count."""
    local = dt.astimezone(IST)
    return local.strftime("%A %-d %b, %-I:%M %p")


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


def subjects_for(coverage: dict, uid: str, source: str) -> set[str]:
    """A user's cached subjects for one source.

    Tolerates the flat list shape written before Taxila and Teams were
    gated separately -- those entries only ever held Taxila subjects.
    """
    entry = coverage.get(uid) or {}
    if isinstance(entry, list):
        return set(entry) if source == "taxila" else set()
    return set(entry.get(source) or [])


def cover_gaps(
    client,
    users: list[dict],
    coverage: dict,
    seen: dict,
    polled: set[str],
    active: set[str],
    poll_one,
) -> dict[str, list[str]]:
    """Re-poll to cover subjects a failed token left unfetched.

    The greedy pass picks a minimum set, so when one of those users fails
    nobody else was asked for their courses. This finds who else the cache
    says holds them and polls those, for that source only.

    Returns whatever is still uncovered. Those subjects are persisted so the
    next run forces the relevant users in from the start -- without that, the
    greedy pass keeps choosing the same failed user and the course stays
    stale indefinitely.
    """
    still: dict[str, list[str]] = {}

    for source in sorted(active):
        expected = {
            s for uid in polled for s in subjects_for(coverage, uid, source)
        }
        got = {s for found in seen.values() for s in (found.get(source) or ())}
        missing = expected - got
        if not missing:
            continue

        log.warning("%s gap after first pass: %s", source, ", ".join(sorted(missing)))

        for user in users:
            uid = str(user["user_id"])
            if uid in polled or not (subjects_for(coverage, uid, source) & missing):
                continue
            poll_one(client, user, {source})
            got |= {s for found in seen.values() for s in (found.get(source) or ())}
            missing = expected - got
            if not missing:
                break

        if missing:
            # Nobody left who can reach these. Logged loudly because the
            # affected courses now hold data that will not refresh.
            log.error(
                "%s still uncovered, data will go stale: %s",
                source,
                ", ".join(sorted(missing)),
            )
            still[source] = sorted(missing)

    return still


def select_users(
    users: list[dict],
    coverage: dict[str, list[str]],
    gaps: dict[str, list[str]] | None = None,
) -> list[dict]:
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

    # Subjects a previous run could not fetch at all. Anyone the cache says
    # holds one is polled regardless of what the greedy pass decides --
    # otherwise a course whose only token died stays stale forever, because
    # minimality keeps choosing the user who already failed.
    wanted = {s for subs in (gaps or {}).values() for s in subs}

    unknown = [u for u in users if str(u["user_id"]) not in coverage]
    known = [u for u in users if str(u["user_id"]) in coverage]

    forced = [
        u for u in known
        if u not in unknown and subjects_of(str(u["user_id"])) & wanted
    ] if wanted else []

    remaining = {s for u in known for s in subjects_of(str(u["user_id"]))}
    chosen = list(unknown) + [u for u in forced if u not in unknown]
    remaining -= {s for u in chosen for s in subjects_of(str(u["user_id"]))}

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


def month_starts(now: datetime, count: int) -> list[datetime]:
    """First-of-month timestamps for this month and the next count-1.

    The endpoint takes one month at a time. Two months is enough at every
    cadence: an event whose partner boundary is further out still carries
    that boundary itself (see pair_events), so nothing is lost by not
    reaching for it.
    """
    out, cursor = [], now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(count):
        out.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return out


def _month(client: httpx.Client, token: str, when: datetime) -> list[dict]:
    """Every event in one calendar month, flattened out of the week grid."""
    data = _ws(
        client,
        token,
        "core_calendar_get_calendar_monthly_view",
        year=when.year,
        month=when.month,
        courseid=1,
    )
    return [
        event
        for week in data.get("weeks", [])
        for day in week.get("days", [])
        for event in day.get("events", [])
    ]


def pair_events(events: list[dict]) -> dict[tuple[str, int], dict]:
    """Collapse Moodle's per-boundary events into one record per activity.

    A quiz emits two events sharing an instance: "Quiz 1 opens"
    (eventtype=open) and "Quiz 1 closes" (eventtype=close). Assignments and
    third-party modules emit a single "due" event.

    Each event also carries the OTHER boundary at day granularity --
    mindaytimestamp on a close/due event is the open day, maxdaytimestamp on
    an open event is the close day. Those are drag-and-drop bounds for the
    calendar UI, not real times, so they are used only when the exact
    partner event is not in the window.
    """
    merged: dict[tuple[str, int], dict] = {}

    for e in events:
        module, instance = e.get("modulename"), e.get("instance")
        if not module or not instance or not e.get("timesort"):
            continue

        key = (module, instance)
        rec = merged.setdefault(
            key,
            {
                "module": module,
                "instance": instance,
                "name": e.get("activityname") or e.get("name"),
                "course": e.get("course") or {},
                "open": None,
                "close": None,
                "open_day": None,
                "close_day": None,
            },
        )

        when = datetime.fromtimestamp(e["timesort"], IST)
        if e.get("eventtype") == "open":
            rec["open"] = when
            if e.get("maxdaytimestamp"):
                rec["close_day"] = datetime.fromtimestamp(e["maxdaytimestamp"], IST)
        else:
            # close, due, and anything else: the deadline moment.
            rec["close"] = when
            if e.get("mindaytimestamp"):
                rec["open_day"] = datetime.fromtimestamp(e["mindaytimestamp"], IST)

        # activityname is absent on some third-party events.
        if not rec["name"]:
            rec["name"] = e.get("activityname") or e.get("name")

    return merged


def resolve_dates(rec: dict) -> tuple[datetime, datetime] | None:
    """(start, end) for a paired record, or None when there is no deadline.

    An open event with no close in the window keeps a provisional end: the
    END of the day maxdaytimestamp names, so the guess can only be late,
    never early. When the close month rolls into range the exact time
    overwrites it -- months before any reminder would fire.
    """
    end = rec["close"]
    if end is None:
        if rec["close_day"] is None:
            return None
        end = rec["close_day"].replace(hour=23, minute=59, second=0, microsecond=0)

    start = rec["open"] or rec["open_day"] or end
    if start > end:
        start = end
    return start, end


def poll_taxila_user(client: httpx.Client, token: str, start: datetime, end: datetime):
    """(rows, subject_keys) for one user, from the monthly calendar view.

    start/end are accepted for signature compatibility with cal_bot.py; the
    months actually fetched are derived from CAL_MONTHS, since the endpoint
    works in whole months.
    """
    events: list[dict] = []
    for when in month_starts(datetime.now(IST), CAL_MONTHS):
        try:
            events += _month(client, token, when)
        except Exception as exc:
            log.warning("month %s-%02d failed: %s", when.year, when.month, exc)

    if not events:
        return [], set()

    paired = pair_events(events)
    limits = quiz_limits(client, token, paired)

    rows, subjects = [], set()
    for rec in paired.values():
        dates = resolve_dates(rec)
        if not dates:
            continue
        start_dt, end_dt = dates

        match = CODE_RE.search((rec["course"] or {}).get("shortname") or "")
        key = match.group(2).upper() if match else "BITS_WILP"
        subjects.add(key)

        module = rec["module"]
        event_type = "quiz" if module == "quiz" else "assignment"
        rows.append(
            {
                "event_key": "%s-%s" % (module, rec["instance"]),
                "source": "taxila",
                "event_type": event_type,
                "subject_key": key,
                "title": rec["name"] or "Untitled",
                "message": compose(rec, key, start_dt, end_dt, limits),
                "start_date": start_dt,
                "end_date": end_dt,
            }
        )

    return rows, subjects


def quiz_limits(
    client: httpx.Client, token: str, paired: dict
) -> dict[int, tuple[int, int]]:
    """{coursemodule: (timelimit_seconds, attempts)} for the quizzes in view.

    The monthly calendar view gives dates but not how long a quiz runs or
    how many attempts are allowed -- both worth knowing before you start one
    fifteen minutes before it closes. One call covers every course, and only
    when the window actually holds a quiz.
    """
    if not QUIZ_ENRICH:
        return {}

    courses = sorted(
        {
            (rec["course"] or {}).get("id")
            for rec in paired.values()
            if rec["module"] == "quiz" and (rec["course"] or {}).get("id")
        }
    )
    if not courses:
        return {}

    try:
        data = _ws(
            client,
            token,
            "mod_quiz_get_quizzes_by_courses",
            **{f"courseids[{i}]": cid for i, cid in enumerate(courses)},
        )
    except Exception as exc:
        # Cosmetic detail only -- the deadline came from the calendar view.
        log.warning("quiz enrich failed: %s", exc)
        return {}

    out = {}
    for q in data.get("quizzes", []):
        if q.get("coursemodule"):
            out[q["coursemodule"]] = (q.get("timelimit") or 0, q.get("attempts") or 0)
    return out


def compose(
    rec: dict,
    subject_key: str,
    start: datetime,
    end: datetime,
    limits: dict[int, tuple[int, int]] | None = None,
) -> str:
    """The line that goes out over WhatsApp.

    Weekday and 12-hour time, because that is how Moodle and the course
    mails write dates and a bare "03 Sep 23:59" makes people count days.
    """
    verb = "closes" if rec["module"] == "quiz" else "is due"
    text = "%s for %s %s %s." % (
        rec["name"] or "Untitled",
        course_name(subject_key),
        verb,
        long_fmt(end),
    )
#### remove if dont wnat to add the opened line enrichement to quiz ##############
    if rec["module"] == "quiz":
        if rec.get("open"):
            text += " Opened %s." % long_fmt(rec["open"])
####################################################################################

        timelimit, attempts = (limits or {}).get(rec["instance"], (0, 0))
        detail = []
        if timelimit:
            minutes = timelimit // 60
            detail.append("%d minute%s" % (minutes, "" if minutes == 1 else "s"))
        if attempts == 1:
            detail.append("one attempt")
        elif attempts > 1:
            detail.append("%d attempts" % attempts)
        if detail:
            text += " " + ", ".join(detail).capitalize() + "."

    return text


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
            WHERE (calendar.title, calendar.message,
                   calendar.start_date, calendar.end_date, calendar.subject_key)
                  IS DISTINCT FROM
                  (EXCLUDED.title, EXCLUDED.message,
                   EXCLUDED.start_date, EXCLUDED.end_date, EXCLUDED.subject_key)
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
    # The day the item opens fires too. On a long window the first
    # milestone can be weeks after submissions opened; this makes sure the
    # group hears about it once when it actually becomes actionable.
    if start is not None:
        days.add(floor.day)

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
                   ON s.echo_title   = c.event_key
                  AND s.target_group = %(grp)s
                  AND s.created_by   = 'echo_cal' 
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
                ON CONFLICT (target_group, echo_title, created_by) DO UPDATE SET
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
            return 0

        coverage_raw = read_state(conn, "taxila_coverage")
        coverage = json.loads(coverage_raw) if coverage_raw else {}
        gaps_raw = read_state(conn, "coverage_gaps")
        gaps = json.loads(gaps_raw) if gaps_raw else {}
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
    outcomes: dict[str, list[str]] = {}
    polled: set[str] = set()

    def poll_one(client, user, sources: set[str]) -> None:
        """Poll one user for the named sources, recording what came back.

        found is tracked per source: a Taxila-only run must not wipe the
        subjects a user's Teams calendar contributed, or the greedy
        selection would think those courses are uncovered.
        """
        uid = str(user["user_id"])
        polled.add(uid)
        errors = outcomes.setdefault(uid, [])
        found: dict[str, set[str]] = {}

        if "taxila" in sources and user.get("wstoken"):
            try:
                r, s = poll_taxila_user(
                    client, user["wstoken"], window_start, window_end
                )
                rows.extend(r)
                found["taxila"] = s
            except Exception as exc:
                # Truncated: the column is a diagnostic, and a Moodle
                # exception body can be long. Never log the token.
                errors.append("taxila: %s" % str(exc)[:200])
                log.warning("taxila poll failed for %s: %s", uid, exc)

        if "teams" in sources and user.get("teams_url"):
            try:
                r, s = poll_teams_user(
                    client, user["teams_url"], window_start, window_end
                )
                rows.extend(r)
                found["teams"] = s
            except Exception as exc:
                errors.append("teams: %s" % str(exc)[:200])
                log.warning("teams poll failed for %s: %s", uid, exc)

        if found:
            merged = seen.setdefault(uid, {})
            merged.update(found)

    active = {s for s, on in (("taxila", do_taxila), ("teams", do_teams)) if on}

    with httpx.Client(timeout=CAL_TIMEOUT_S, follow_redirects=True) as client:
        for user in select_users(users, coverage, gaps):
            poll_one(client, user, active)

        # A failed token leaves that user's subjects unfetched, and the
        # greedy pass had already decided nobody else needed polling for
        # them. Cover the hole with whoever else the cache says has it,
        # rather than letting a course go stale until the token is fixed.
        gaps = cover_gaps(client, users, coverage, seen, polled, active, poll_one)

    record_poll([(uid, "; ".join(errs) if errs else None) for uid, errs in outcomes.items()])

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
        # Persisted so the next run forces in whoever can cover them. An
        # empty dict clears a gap that has since been filled.
        write_state(conn, "coverage_gaps", json.dumps(gaps))
        if do_taxila:
            write_state(conn, "taxila_last_poll", taxila_bucket)
        if do_teams:
            write_state(conn, "teams_last_poll", teams_bucket)
        log.info("calendar rows written: %d", written)

        sync_notices(conn)
    finally:
        conn.close()

    return 0
