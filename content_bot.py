#!/usr/bin/env python3
"""
Course content sync. Runs from run.py, after course_bot.

Fills dim_course_content: one row per module in a current course --
handouts, slides, past papers, forums, quizzes -- with whatever files and
dates that module happens to carry.

The shape is deliberately loose. A resource has one PDF and no dates, a
folder has many files or none, a quiz has two dates and no files, a forum
has neither. Fixed columns for each would break the first time a course did
something unexpected, so files go in one JSONB column and dates in one text
line. The point is to hand a whole course to a model and let it answer the
question, not to query artefacts individually.

Keyed on course, not user: two registered students in the same course sync
it once. Current courses only -- enddate in the future -- so labware and
finished semesters are not re-fetched daily.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from echo_bot import IST, connect, env_bool

log = logging.getLogger("content_bot")

TAXILA_API_URL = os.environ.get(
    "TAXILA_API_URL", "https://taxila-aws.bits-pilani.ac.in/webservice/rest/server.php"
)

CONTENT_SYNC_ENABLED = env_bool("CONTENT_SYNC_ENABLED", True)
CONTENT_REFRESH_INTERVAL_H = int(os.environ.get("CONTENT_REFRESH_INTERVAL_H", "24"))
CONTENT_TIMEOUT_S = float(os.environ.get("CONTENT_TIMEOUT_S", "60"))
CONTENT_MAX_COURSES = int(os.environ.get("CONTENT_MAX_COURSES", "10"))


def fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, IST).strftime("%d %b %Y %H:%M")


def describe_dates(dates: list[dict]) -> str | None:
    """"Opened: 12 Aug 2026 09:00; Closed: 15 Aug 2026 23:59"

    Built from label, not dataid: a quiz carries dataid=timeopen/timeclose
    and an assignment carries allowsubmissionsfromdate/duedate, but
    groupselect carries neither -- only the label. Labels are present on all
    three, so they are the reliable key.

    Kept as text rather than columns because calendar already holds exact
    deadlines from a verified source; duplicating them here would give two
    places to disagree. This is context for a model, not a query surface.
    """
    parts = []
    for d in dates or []:
        stamp = d.get("timestamp")
        if not stamp:
            continue
        label = (d.get("label") or "").strip().rstrip(":")
        parts.append("%s: %s" % (label or "Date", fmt(stamp)))
    return "; ".join(parts) or None


def files_of(module: dict) -> list[dict]:
    """Every file on a module, flattened.

    A resource has one, a folder has many or none, a forum has no contents
    key at all. fileurl is stored as Moodle returns it -- already carrying
    forcedownload -- because a logged-in student opening it in a browser
    resolves fine without a token.
    """
    out = []
    for item in module.get("contents") or []:
        if item.get("type") != "file" or not item.get("fileurl"):
            continue
        out.append(
            {
                "name": item.get("filename"),
                "url": item["fileurl"],
                "mime": item.get("mimetype"),
                "size": item.get("filesize"),
            }
        )
    return out


def rows_for_course(course_id: int, sections: list[dict]) -> list[dict]:
    """One row per module. Empty sections contribute nothing."""
    rows = []
    for section in sections:
        for module in section.get("modules") or []:
            if not module.get("id"):
                continue
            rows.append(
                {
                    "course_id": course_id,
                    "module_id": module["id"],
                    "section_no": section.get("section"),
                    "section_name": section.get("name"),
                    "module_name": module.get("name") or "Untitled",
                    "modname": module.get("modname") or "unknown",
                    "url": module.get("url"),
                    "files": Jsonb(files_of(module)),
                    "dates": describe_dates(module.get("dates")),
                }
            )
    return rows


# ---------------------------------------------------------------------------


def pending_courses(conn: psycopg.Connection) -> list[dict]:
    """Current courses whose content is missing or stale.

    Distinct on course_id, so a course two users share is fetched once. Any
    active user enrolled in it supplies the token -- content is course-level,
    and whichever token is used sees essentially the same page.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (c.course_id)
                   c.course_id, c.shortname, u.wstoken
            FROM dim_taxila_course c
            JOIN dim_taxila_usr u ON u.user_id = c.user_id
            LEFT JOIN (
                SELECT course_id, max(synced_at) AS synced_at
                FROM dim_course_content GROUP BY course_id
            ) s ON s.course_id = c.course_id
            WHERE COALESCE(u.is_active, TRUE)
              AND u.wstoken IS NOT NULL
              AND c.kind = 'subject'
              AND c.enddate > now()
              AND (s.synced_at IS NULL
                   OR s.synced_at < now() - make_interval(hours => %s))
            ORDER BY c.course_id, s.synced_at NULLS FIRST
            LIMIT %s
            """,
            (CONTENT_REFRESH_INTERVAL_H, CONTENT_MAX_COURSES),
        )
        return cur.fetchall()


def fetch_contents(client: httpx.Client, token: str, course_id: int) -> list[dict]:
    r = client.get(
        TAXILA_API_URL,
        params={
            "wstoken": token,
            "wsfunction": "core_course_get_contents",
            "moodlewsrestformat": "json",
            "courseid": course_id,
        },
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(data.get("errorcode", "unknown"))
    return data if isinstance(data, list) else []


def store(conn: psycopg.Connection, course_id: int, rows: list[dict]) -> None:
    """Upsert modules and drop ones the course no longer has."""
    with conn.cursor() as cur:
        if rows:
            cur.executemany(
                """
                INSERT INTO dim_course_content (course_id, module_id, section_no,
                        section_name, module_name, modname, url, files, dates)
                VALUES (%(course_id)s, %(module_id)s, %(section_no)s,
                        %(section_name)s, %(module_name)s, %(modname)s, %(url)s,
                        %(files)s, %(dates)s)
                ON CONFLICT (course_id, module_id) DO UPDATE SET
                    section_no   = EXCLUDED.section_no,
                    section_name = EXCLUDED.section_name,
                    module_name  = EXCLUDED.module_name,
                    modname      = EXCLUDED.modname,
                    url          = EXCLUDED.url,
                    files        = EXCLUDED.files,
                    dates        = EXCLUDED.dates,
                    synced_at    = now()
                WHERE (dim_course_content.module_name, dim_course_content.url,
                       dim_course_content.files, dim_course_content.dates,
                       dim_course_content.section_name)
                      IS DISTINCT FROM
                      (EXCLUDED.module_name, EXCLUDED.url, EXCLUDED.files,
                       EXCLUDED.dates, EXCLUDED.section_name)
                """,
                rows,
            )
        cur.execute(
            "DELETE FROM dim_course_content "
            "WHERE course_id = %s AND NOT (module_id = ANY(%s))",
            (course_id, [r["module_id"] for r in rows]),
        )
        if cur.rowcount:
            log.info("dropped %d removed module(s) from %s", cur.rowcount, course_id)
    conn.commit()


# ---------------------------------------------------------------------------


def main() -> int:
    if not CONTENT_SYNC_ENABLED:
        return 0

    conn = connect()
    try:
        courses = pending_courses(conn)
    finally:
        conn.close()

    if not courses:
        return 0

    log.info("content sync for %d course(s)", len(courses))

    fetched: list[tuple[int, list[dict]]] = []
    with httpx.Client(timeout=CONTENT_TIMEOUT_S) as client:
        for course in courses:
            cid = course["course_id"]
            try:
                sections = fetch_contents(client, course["wstoken"], cid)
                fetched.append((cid, rows_for_course(cid, sections)))
            except Exception as exc:
                # Unstamped -- synced_at comes from the rows themselves, so a
                # failed course is simply still stale and retried next run.
                log.warning("contents failed for %s: %s", course["shortname"], exc)

    conn = connect()
    try:
        for cid, rows in fetched:
            store(conn, cid, rows)
            files = sum(len(r["files"].obj) for r in rows)
            log.info("course %s: %d module(s), %d file(s)", cid, len(rows), files)
    finally:
        conn.close()

    return 0
