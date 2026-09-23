#!/usr/bin/env python3
"""
Course enrolment sync. Runs from run.py, before the bots that depend on it.

Fills dim_taxila_course: which courses each registered user is enrolled in,
what kind each is, and when it ends. Everything downstream reads that table
rather than calling Moodle again -- content_bot and group_bot both need
course ids, and enddate is what tells them which courses are current.

Monthly, not daily: enrolments change at semester boundaries and almost
never in between. A user with course_synced_at NULL has just registered and
is picked up on the next run regardless of the interval.

Ordering matters. A new user has no courses until this runs, so content and
group sync can do nothing for them until it has. Running first means a
registration completes in one pass instead of three.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import httpx
import psycopg
from psycopg.rows import dict_row

from echo_bot import IST, connect

log = logging.getLogger("course_bot")

TAXILA_API_URL = os.environ.get(
    "TAXILA_API_URL", "https://taxila-aws.bits-pilani.ac.in/webservice/rest/server.php"
)

COURSE_SYNC_ENABLED = os.environ.get("COURSE_SYNC_ENABLED", "1") not in ("0", "false", "")
COURSE_REFRESH_INTERVAL_H = int(os.environ.get("COURSE_REFRESH_INTERVAL_H", "720"))
COURSE_TIMEOUT_S = float(os.environ.get("COURSE_TIMEOUT_S", "30"))
COURSE_MAX_USERS = int(os.environ.get("COURSE_MAX_USERS", "10"))

# S1-26_AIMLZG521 -> semester S1-26, subject AIMLZG521
SUBJECT_RE = re.compile(r"^(S\d-\d{2})_(AIML[A-Z]*\d{3})$", re.IGNORECASE)


def classify(shortname: str) -> tuple[str, str | None, str | None]:
    """(kind, subject_key, semester) from the course shortname.

        S1-26_AIMLZG521  -> subject,  AIMLZG521, S1-26
        LW_DNN           -> labware,  None,      None
        OSR-S1-25        -> resource, None,      None

    Anything unrecognised is a resource rather than an error: the catalogue
    gains odd entries (orientation recordings, one-off workshops) and none
    of them should stop a sync.
    """
    name = (shortname or "").strip()
    match = SUBJECT_RE.match(name)
    if match:
        return "subject", match.group(2).upper(), match.group(1).upper()
    if name.upper().startswith("LW_"):
        return "labware", None, None
    return "resource", None, None


def ts(epoch: int | None) -> datetime | None:
    """Moodle sends 0 for "no date" -- labware and resources have no end."""
    return datetime.fromtimestamp(epoch, IST) if epoch else None


# ---------------------------------------------------------------------------


def pending_users(conn: psycopg.Connection) -> list[dict]:
    """Users never synced, or due again. NULL is the new-registration case."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT user_id, wstoken
            FROM dim_taxila_usr
            WHERE COALESCE(is_active, TRUE)
              AND wstoken IS NOT NULL
              AND (course_synced_at IS NULL
                   OR course_synced_at < now() - make_interval(hours => %s))
            ORDER BY course_synced_at NULLS FIRST
            LIMIT %s
            """,
            (COURSE_REFRESH_INTERVAL_H, COURSE_MAX_USERS),
        )
        return cur.fetchall()


def fetch_courses(client: httpx.Client, token: str, user_id: str) -> list[dict]:
    r = client.get(
        TAXILA_API_URL,
        params={
            "wstoken": token,
            "wsfunction": "core_enrol_get_users_courses",
            "moodlewsrestformat": "json",
            "userid": user_id,
        },
    )
    r.raise_for_status()
    data = r.json()
    # Moodle answers 200 with an exception body rather than an HTTP error.
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(data.get("errorcode", "unknown"))
    return data if isinstance(data, list) else []


def rows_for(user_id: str, courses: list[dict]) -> list[dict]:
    out = []
    for c in courses:
        if not c.get("id"):
            continue
        kind, subject_key, semester = classify(c.get("shortname", ""))
        out.append(
            {
                "user_id": user_id,
                "course_id": c["id"],
                "subject_key": subject_key,
                "semester": semester,
                "kind": kind,
                "fullname": c.get("fullname") or c.get("shortname") or "Untitled",
                "shortname": c.get("shortname") or "",
                "startdate": ts(c.get("startdate")),
                "enddate": ts(c.get("enddate")),
            }
        )
    return out


def store(conn: psycopg.Connection, user_id: str, rows: list[dict]) -> None:
    """Upsert this user's courses and drop the ones they have left.

    The delete is scoped to this user: another user's enrolment in the same
    course is a separate row and must survive.
    """
    with conn.cursor() as cur:
        if rows:
            cur.executemany(
                """
                INSERT INTO dim_taxila_course (user_id, course_id, subject_key,
                        semester, kind, fullname, shortname, startdate, enddate)
                VALUES (%(user_id)s, %(course_id)s, %(subject_key)s, %(semester)s,
                        %(kind)s, %(fullname)s, %(shortname)s, %(startdate)s,
                        %(enddate)s)
                ON CONFLICT (user_id, course_id) DO UPDATE SET
                    subject_key = EXCLUDED.subject_key,
                    semester    = EXCLUDED.semester,
                    kind        = EXCLUDED.kind,
                    fullname    = EXCLUDED.fullname,
                    shortname   = EXCLUDED.shortname,
                    startdate   = EXCLUDED.startdate,
                    enddate     = EXCLUDED.enddate,
                    synced_at   = now()
                """,
                rows,
            )
        cur.execute(
            "DELETE FROM dim_taxila_course "
            "WHERE user_id = %s AND NOT (course_id = ANY(%s))",
            (user_id, [r["course_id"] for r in rows]),
        )
        if cur.rowcount:
            log.info("dropped %d unenrolled course(s) for %s", cur.rowcount, user_id)

        cur.execute(
            "UPDATE dim_taxila_usr SET course_synced_at = now() WHERE user_id = %s",
            (user_id,),
        )
    conn.commit()


# ---------------------------------------------------------------------------


def main() -> int:
    if not COURSE_SYNC_ENABLED:
        return 0

    conn = connect()
    try:
        users = pending_users(conn)
    finally:
        conn.close()

    if not users:
        return 0

    log.info("course sync for %d user(s)", len(users))

    # Fetch with no connection held, then write.
    fetched: list[tuple[str, list[dict]]] = []
    with httpx.Client(timeout=COURSE_TIMEOUT_S) as client:
        for user in users:
            uid = str(user["user_id"])
            try:
                courses = fetch_courses(client, user["wstoken"], uid)
                fetched.append((uid, rows_for(uid, courses)))
            except Exception as exc:
                # Unstamped, so retried next run rather than waiting out the
                # month.
                log.warning("course fetch failed for %s: %s", uid, exc)

    conn = connect()
    try:
        for uid, rows in fetched:
            store(conn, uid, rows)
            subjects = sum(1 for r in rows if r["kind"] == "subject")
            log.info("%s: %d course(s), %d subject(s)", uid, len(rows), subjects)
    finally:
        conn.close()

    return 0
