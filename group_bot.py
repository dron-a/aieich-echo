#!/usr/bin/env python3
"""
Group membership sync. Runs from run.py, after the calendar bots.

Answers "who is in my group" for a registered user, in any course where
they belong to one.

Two triggers, both expressed as one query: a user with group_synced_at NULL
has never been synced (they just registered), and a user whose stamp is
older than GROUP_REFRESH_INTERVAL_H is due again. On hours where neither
applies, this does one indexed lookup and returns -- no HTTP, no writes.

The refresh matters because group self-selection stays open for days after a
course does. A user synced the hour they registered would otherwise show an
empty group permanently.

Two calls per course, both narrow. core_group_get_course_user_groups gives
the user's own group ids; core_enrol_get_enrolled_users then returns just
that group's members, filtered server side.

The unfiltered form would be one call instead of two, but Conversational AI
alone has 582 enrolled users and every entry carries roles, enrolled
courses and profile URLs -- megabytes to fetch and discard for the sake of
a four-person project group. groupid narrows the rows, userfields narrows
the columns to the three that are stored.

The direct group-members call is blocked for students here. groupid works
because Moodle only demands accessallgroups when the caller is NOT in the
group being asked about.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx
import psycopg
from psycopg.rows import dict_row

from echo_bot import IST, connect

log = logging.getLogger("group_bot")

TAXILA_API_URL = os.environ.get(
    "TAXILA_API_URL", "https://taxila-aws.bits-pilani.ac.in/webservice/rest/server.php"
)

GROUP_SYNC_ENABLED = os.environ.get("GROUP_SYNC_ENABLED", "1") not in ("0", "false", "")
GROUP_REFRESH_INTERVAL_H = int(os.environ.get("GROUP_REFRESH_INTERVAL_H", "24"))
GROUP_TIMEOUT_S = float(os.environ.get("GROUP_TIMEOUT_S", "30"))
GROUP_MAX_USERS = int(os.environ.get("GROUP_MAX_USERS", "10"))

# Every account on this instance -- students, faculty and staff -- uses the
# same mail domain, and the local part is the BITS id.
BITS_DOMAIN = os.environ.get("BITS_MAIL_DOMAIN", "wilp.bits-pilani.ac.in")


def bits_id_of(email: str | None) -> str | None:
    """BITS id from the address: 2025ae05184@... -> 2025AE05184.

    Derived rather than read from the payload's idnumber field: Moodle
    returns idnumber, username and department only for the account whose
    token made the call, so using it would populate one member of a group
    and leave the rest null for no reason a reader could interpret.
    """
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if domain.lower() != BITS_DOMAIN:
        return None
    return local.upper() or None


# ---------------------------------------------------------------------------
# Who needs syncing
# ---------------------------------------------------------------------------


def pending_users(conn: psycopg.Connection) -> list[dict]:
    """Users never synced, or stale. NULL covers the new-registration case."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT user_id, wstoken
            FROM dim_taxila_usr
            WHERE COALESCE(is_active, TRUE)
              AND wstoken IS NOT NULL
              AND (group_synced_at IS NULL
                   OR group_synced_at < now() - make_interval(hours => %s))
            ORDER BY group_synced_at NULLS FIRST
            LIMIT %s
            """,
            (GROUP_REFRESH_INTERVAL_H, GROUP_MAX_USERS),
        )
        return cur.fetchall()


def course_ids_for(conn: psycopg.Connection, user_id: str) -> list[int]:
    """Current subject courses only.

    Without the filter this returns every enrolment -- a finished semester,
    four labware courses and an orientation archive -- which is thirteen
    courses where four matter, and three calls each. None of the excluded
    ones carries a project group worth refreshing, and last term's groups
    are history rather than something that changes.

    Rows already written for an excluded course stay; they simply stop
    being refreshed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT course_id FROM dim_taxila_course "
            "WHERE user_id = %s AND kind = 'subject' AND enddate > now()",
            (user_id,),
        )
        return [r[0] for r in cur.fetchall()]


def stamp(conn: psycopg.Connection, user_ids: list[str]) -> None:
    """Mark these users synced.

    Stamped whether or not they turned out to have groups -- a user in no
    group writes no rows, and without the stamp they would look unsynced
    forever and be re-fetched every hour.
    """
    if not user_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dim_taxila_usr SET group_synced_at = now() "
            "WHERE user_id = ANY(%s)",
            (user_ids,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _ws(client: httpx.Client, token: str, fn: str, **params):
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


def my_groups(client: httpx.Client, token: str, course_id: int, user_id: int) -> list[dict]:
    """The caller's own groups in one course."""
    data = _ws(
        client,
        token,
        "core_group_get_course_user_groups",
        courseid=course_id,
        userid=user_id,
    )
    return (data or {}).get("groups") or []


def group_members(
    client: httpx.Client, token: str, course_id: int, group_id: int
) -> list[dict]:
    """Members of one group, three fields each.

    Both options matter. groupid filters server side, so a 582-user course
    returns the four people actually wanted. userfields drops roles,
    enrolled courses, profile URLs and preferences -- none of which is
    stored, and together far larger than what is.

    Unrecognised option names are ignored silently rather than rejected, so
    a typo here would quietly return the whole course.
    """
    data = _ws(
        client,
        token,
        "core_enrol_get_enrolled_users",
        courseid=course_id,
        **{
            "options[0][name]": "groupid",
            "options[0][value]": group_id,
            "options[1][name]": "userfields",
            "options[1][value]": "id,fullname,email",
        },
    )
    return data if isinstance(data, list) else []


def rows_for_group(
    people: list[dict], course_id: int, group_id: int, group_name: str
) -> list[dict]:
    """Membership rows for one group.

    No filtering here: the groupid option already guarantees every returned
    user is in this group.
    """
    rows = []
    for person in people:
        if not person.get("id"):
            continue
        rows.append(
            {
                "course_id": course_id,
                "group_id": group_id,
                "group_name": group_name,
                "user_id": person["id"],
                "fullname": (person.get("fullname") or "").strip() or "Unknown",
                "bits_id": bits_id_of(person.get("email")),
                "email": person.get("email"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def upsert(conn: psycopg.Connection, rows: list[dict]) -> int:
    """Write memberships.

    Keyed on (group_id, user_id), so a group shared by two registered users
    is stored once rather than once per registered member. synced_at only
    moves when something actually changed.
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO group_memberships (course_id, group_id, group_name,
                                           user_id, fullname, bits_id, email)
            VALUES (%(course_id)s, %(group_id)s, %(group_name)s,
                    %(user_id)s, %(fullname)s, %(bits_id)s, %(email)s)
            ON CONFLICT (group_id, user_id) DO UPDATE SET
                course_id  = EXCLUDED.course_id,
                group_name = EXCLUDED.group_name,
                fullname   = EXCLUDED.fullname,
                bits_id    = EXCLUDED.bits_id,
                email      = EXCLUDED.email,
                synced_at  = now()
            WHERE (group_memberships.group_name, group_memberships.fullname,
                   group_memberships.bits_id, group_memberships.email)
                  IS DISTINCT FROM
                  (EXCLUDED.group_name, EXCLUDED.fullname,
                   EXCLUDED.bits_id, EXCLUDED.email)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------


def main() -> int:
    if not GROUP_SYNC_ENABLED:
        return 0

    conn = connect()
    try:
        users = pending_users(conn)
        if not users:
            return 0
        work = [(u["user_id"], u["wstoken"], course_ids_for(conn, u["user_id"]))
                for u in users]
    finally:
        conn.close()

    log.info("group sync for %d user(s)", len(work))

    # All HTTP with no database connection held.
    rows: list[dict] = []
    done: list[str] = []

    with httpx.Client(timeout=GROUP_TIMEOUT_S) as client:
        for user_id, token, course_ids in work:
            ok = True
            for course_id in course_ids:
                try:
                    groups = my_groups(client, token, course_id, int(user_id))
                except Exception as exc:
                    ok = False
                    log.warning("groups failed for %s course %s: %s",
                                user_id, course_id, exc)
                    continue

                # A course where this user is in no group costs one call and
                # writes nothing -- which is most labware and resources.
                for group in groups:
                    gid = group.get("id")
                    if not gid:
                        continue
                    try:
                        people = group_members(client, token, course_id, gid)
                    except Exception as exc:
                        ok = False
                        log.warning("members failed for group %s: %s", gid, exc)
                        continue
                    rows += rows_for_group(
                        people, course_id, gid, group.get("name") or str(gid)
                    )
            # Stamped only on a clean pass, so a user whose fetch failed is
            # retried next hour instead of waiting out the refresh window.
            if ok:
                done.append(user_id)

    # Two registered users in one course produce the same membership rows.
    unique = {(r["group_id"], r["user_id"]): r for r in rows}
    log.info("%d membership rows (%d after dedup)", len(rows), len(unique))

    conn = connect()
    try:
        upsert(conn, list(unique.values()))
        stamp(conn, done)
    finally:
        conn.close()

    return 0