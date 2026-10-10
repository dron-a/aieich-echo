#!/usr/bin/env python3
"""
Course announcements, read from each course's Announcements forum.

Runs from run.py after group_bot. Sends new announcements to the group the
same way mail_bot sends mail -- an LLM summary -- with the post's files and
links appended by code, never through the model.

Who is polled
-------------
Students see different posts depending on their groups, so the poll is
chosen per course:

  course with section groups   one registered user per section
  course without sections      any one registered user

"Registered" means in dim_taxila_usr, active, with a token -- membership
alone is useless without a token to poll as. Users whose groups have never
been synced are polled too, since nothing is known about their sections.
Everything a polled user can see is sent: posts to everyone, to their
section, to their own group.

Dedup and first run
-------------------
Every post is recorded in calendar as an announcement, keyed forum-<id>
on the discussion id, which is unique across Taxila. A post already there
is never sent again.

The first ever run records everything and sends nothing -- mail has already
announced those (bot_state key forum_baselined). After that, an unrecorded
post is sent only if it is under FORUM_LOOKBACK_HOURS old. That second rule
is for a new registrant revealing their section's backlog: recorded, not
blasted to the group.

Not handled: replies inside an announcement thread, and edits to a post
already sent.
"""

from __future__ import annotations

import html
import importlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import httpx
import psycopg
from psycopg.rows import dict_row

from content_bot import public_file_url
from echo_bot import (
    API_KEY,
    API_URL,
    INSTANCE,
    IST,
    SEND_TIMEOUT_S,
    connect,
    env_bool,
    log_send,
    send_attempt,
)
from group_bot import is_section
from mail_bot import MAIL_TARGET_GROUP, SUBJECT_NAMES, summarise

log = logging.getLogger("forum_bot")

# Shares state helpers and upsert with whichever calendar module run.py uses.
_cal = importlib.import_module(os.environ.get("CALENDAR_MODULE", "cal_bot_monthly"))
read_state, write_state, upsert = _cal.read_state, _cal.write_state, _cal.upsert

TAXILA_SITE = os.environ.get("TAXILA_SITE", "https://taxila-aws.bits-pilani.ac.in")
TAXILA_API_URL = os.environ.get(
    "TAXILA_API_URL", TAXILA_SITE + "/webservice/rest/server.php"
)

FORUM_ENABLED = env_bool("FORUM_ENABLED", True)
FORUM_LOOKBACK_HOURS = int(os.environ.get("FORUM_LOOKBACK_HOURS", "48"))
FORUM_PERPAGE = int(os.environ.get("FORUM_PERPAGE", "10"))
FORUM_TIMEOUT_S = float(os.environ.get("FORUM_TIMEOUT_S", "30"))
FORUM_TEXT_CHARS = int(os.environ.get("FORUM_TEXT_CHARS", "4000"))

EVERYONE = -1


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def to_text(markup: str) -> str:
    """Plain text from a post's HTML, keeping paragraphs and list items."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", markup or "")
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def attachments(post: dict) -> list[tuple[str, str]]:
    """(name, url) for every file on a post, in browser-openable form.

    Files embedded in the message arrive as messageinlinefiles, and the
    attachment flag stays false for them -- so both lists are read.
    """
    out, seen = [], set()
    for f in (post.get("messageinlinefiles") or []) + (post.get("attachments") or []):
        url = public_file_url(f.get("fileurl"))
        if url and url not in seen:
            seen.add(url)
            out.append((f.get("filename") or "file", url))
    return out


def links(post: dict, exclude: set[str]) -> list[str]:
    """Plain web links in the message, excluding files already listed.

    Images are ignored -- the ones seen so far are broken Gmail embeds
    pasted into the editor, not content.
    """
    out, seen = [], set(exclude)
    for href in re.findall(r'href="([^"]+)"', post.get("message") or ""):
        url = public_file_url(html.unescape(href).strip())
        if not url.startswith(("http://", "https://")) or "mail.google.com" in url:
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# Who to poll
# ---------------------------------------------------------------------------


def plan(conn: psycopg.Connection) -> tuple[list[dict], dict[int, str], dict[int, dict]]:
    """Pollers per current course, section names, and course details.

    Returns (polls, section_names, courses):
      polls          [{user_id, wstoken, course_id}] -- one entry per poll
      section_names  {group_id: name} for every known section group
      courses        {course_id: {shortname, subject_key, fullname}}
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT c.course_id, c.shortname, c.subject_key, c.fullname,
                   u.user_id, u.wstoken, u.group_synced_at
            FROM dim_taxila_course c
            JOIN dim_taxila_usr u ON u.user_id = c.user_id
            WHERE c.kind = 'subject'
              AND c.enddate > now()
              AND COALESCE(u.is_active, TRUE)
              AND u.wstoken IS NOT NULL
            """
        )
        enrolled = cur.fetchall()

        cur.execute(
            "SELECT course_id, group_id, group_name, user_id FROM group_memberships"
        )
        memberships = cur.fetchall()

    def uid_key(u: str) -> int:
        try:
            return int(u)
        except (TypeError, ValueError):
            return 1 << 62

    by_course: dict[int, dict[str, dict]] = {}
    courses: dict[int, dict] = {}
    for r in enrolled:
        by_course.setdefault(r["course_id"], {})[str(r["user_id"])] = r
        courses[r["course_id"]] = {
            "shortname": r["shortname"],
            "subject_key": r["subject_key"],
            "fullname": r["fullname"],
        }

    section_names: dict[int, str] = {}
    sections: dict[int, dict[int, set[str]]] = {}   # course -> section -> users
    for m in memberships:
        if not is_section(m["group_name"]):
            continue
        section_names[m["group_id"]] = m["group_name"]
        sections.setdefault(m["course_id"], {}).setdefault(m["group_id"], set()).add(
            str(m["user_id"])
        )

    polls = []
    for course_id, users in by_course.items():
        chosen: set[str] = set()
        course_sections = {
            gid: members & set(users)
            for gid, members in sections.get(course_id, {}).items()
        }
        course_sections = {g: m for g, m in course_sections.items() if m}

        if course_sections:
            # One per section, the lowest user id, so the same person is
            # polled every run.
            for members in course_sections.values():
                chosen.add(min(members, key=uid_key))
        else:
            chosen.add(min(users, key=uid_key))

        # Groups never synced -- their sections are unknown.
        chosen |= {u for u, r in users.items() if r["group_synced_at"] is None}

        for u in sorted(chosen, key=uid_key):
            polls.append(
                {"user_id": u, "wstoken": users[u]["wstoken"], "course_id": course_id}
            )
    return polls, section_names, courses


# ---------------------------------------------------------------------------
# Moodle
# ---------------------------------------------------------------------------


def _ws(client: httpx.Client, token: str, fn: str, **params):
    r = client.get(
        TAXILA_API_URL,
        params={"wstoken": token, "wsfunction": fn, "moodlewsrestformat": "json", **params},
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError("%s: %s" % (fn, data.get("errorcode", "unknown")))
    return data


def announcement_forums(
    client: httpx.Client, token: str, course_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """{course_id: (forum_id, cmid)} for each course's Announcements forum.

    Identified by type "news", Moodle's internal type for it, not by name.
    """
    data = _ws(
        client, token, "mod_forum_get_forums_by_courses",
        **{"courseids[%d]" % i: c for i, c in enumerate(course_ids)},
    )
    out = {}
    for f in data or []:
        if f.get("type") == "news" and f.get("course") and f.get("id"):
            out[f["course"]] = (f["id"], f.get("cmid") or 0)
    return out


def discussions(client: httpx.Client, token: str, forum_id: int) -> list[dict]:
    data = _ws(
        client, token, "mod_forum_get_forum_discussions",
        forumid=forum_id, perpage=FORUM_PERPAGE,
    )
    return (data or {}).get("discussions") or []


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def course_header(course: dict) -> str:
    """Same header shape as mail notifications: shortname plus course name."""
    name = SUBJECT_NAMES.get(course.get("subject_key") or "")
    if not name:
        name = re.sub(r"\s*\([^)]*\)\s*$", "", course.get("fullname") or "").strip()
    return ("%s %s" % (course.get("shortname") or "", name or "")).strip()


def compose(post: dict, item: dict | None, course: dict, section: str | None,
            cmid: int) -> str:
    title = html.unescape(post.get("subject") or post.get("name") or "Announcement")
    header = "*%s*" % course_header(course)
    if section:
        header += " · " + section

    if item:
        topic, summary = item["topic"], item["summary"]
    else:
        # No usable summary for this post -- fall back to its own words.
        text = to_text(post.get("message"))
        topic, summary = title, (text[:500] + "…") if len(text) > 500 else text

    parts = [header, topic, summary]

    files = attachments(post)
    if files:
        parts.append("Files:\n" + "\n".join("- %s\n  %s" % f for f in files))

    extra = links(post, {u for _, u in files})
    if extra:
        parts.append("Links:\n" + "\n".join("- " + u for u in extra))

    if post.get("groupid", EVERYONE) == EVERYONE:
        parts.append("%s/mod/forum/discuss.php?d=%d" % (TAXILA_SITE, post["discussion"]))
    elif cmid:
        # A group's thread opens only for that group; the forum page shows
        # each student their own copy.
        parts.append("%s/mod/forum/view.php?id=%d" % (TAXILA_SITE, cmid))

    return "\n".join(p for p in parts if p)


def calendar_row(post: dict, course: dict) -> dict:
    """The record that marks a post as seen, and gives agents its text."""
    now = datetime.now(IST)
    created = datetime.fromtimestamp(post.get("created") or 0, IST) if post.get("created") else now
    when = min(created, now)
    text = to_text(post.get("message"))
    names = [n for n, _ in attachments(post)]
    if names:
        text += "\n\nFiles: " + ", ".join(names)
    return {
        "event_key": "forum-%d" % post["discussion"],
        "source": "taxila",
        "event_type": "announcement",
        "subject_key": course.get("subject_key") or "BITS_WILP",
        "title": html.unescape(post.get("subject") or post.get("name") or "Announcement"),
        "message": text[:FORUM_TEXT_CHARS],
        "start_date": when,
        "end_date": when,
    }


def _retryable(status: int) -> bool:
    return status >= 500 or status == 429


def send(messages: list[tuple[dict, str]]) -> list[dict]:
    """Send in order; return the posts that are finished.

    Same rules as mail: a temporary failure stops the batch and leaves the
    rest unrecorded, so they retry next run; a permanent rejection is
    recorded and skipped, so it cannot block everything behind it.
    """
    done = []
    with httpx.Client(base_url=API_URL, headers={"apikey": API_KEY},
                      timeout=SEND_TIMEOUT_S) as client:
        for post, text in messages:
            ident = "forum d=%d" % post["discussion"]
            tags = send_attempt()
            started = time.monotonic()
            try:
                r = client.post("/message/sendText/" + INSTANCE,
                                json={"number": MAIL_TARGET_GROUP, "text": text})
            except httpx.TransportError as exc:
                log_send(ident, time.monotonic() - started, tags, exc=exc)
                log.info("forum d=%d held for retry next run", post["discussion"])
                break
            if log_send(ident, time.monotonic() - started, tags, r=r):
                done.append(post)
            elif _retryable(r.status_code):
                log.info("forum d=%d held for retry next run", post["discussion"])
                break
            else:
                log.info("forum d=%d skipped -- rejected, retrying would not help",
                         post["discussion"])
                done.append(post)
    return done


# ---------------------------------------------------------------------------


def main() -> int:
    if not FORUM_ENABLED:
        log.info("disabled (FORUM_ENABLED off)")
        return 0
    if not MAIL_TARGET_GROUP:
        log.error("forum bot not configured, missing MAIL_TARGET_GROUP")
        return 0

    conn = connect()
    try:
        polls, section_names, courses = plan(conn)
        baselined = read_state(conn, "forum_baselined") is not None
        cached = {int(k): tuple(v) for k, v in
                  json.loads(read_state(conn, "forum_ids") or "{}").items()}
    finally:
        conn.close()

    if not polls:
        log.info("nothing to poll -- no current subject course with a registered user")
        return 0
    log.info("polling %d forum view(s) across %d course(s)",
             len(polls), len({p["course_id"] for p in polls}))

    # --- fetch, no connection held ---------------------------------------
    forum_ids = dict(cached)
    found: dict[int, tuple[dict, int]] = {}   # discussion -> (post, course_id)
    ok = 0

    with httpx.Client(timeout=FORUM_TIMEOUT_S) as client:
        # Forum ids never change, so only courses not yet cached cost a call.
        missing: dict[str, tuple[str, list[int]]] = {}
        for p in polls:
            if p["course_id"] not in forum_ids:
                missing.setdefault(p["user_id"], (p["wstoken"], []))[1].append(p["course_id"])
        for uid, (token, course_ids) in missing.items():
            try:
                forum_ids.update(announcement_forums(client, token, sorted(set(course_ids))))
            except Exception as exc:
                log.warning("forum list failed for %s: %s", uid, exc)

        for p in polls:
            forum = forum_ids.get(p["course_id"])
            if not forum:
                continue
            try:
                for post in discussions(client, p["wstoken"], forum[0]):
                    if post.get("discussion"):
                        found.setdefault(post["discussion"], (post, p["course_id"]))
                ok += 1
            except Exception as exc:
                log.warning("discussions failed for %s course %s: %s",
                            p["user_id"], p["course_id"], exc)

    # --- what is new ------------------------------------------------------
    conn = connect()
    try:
        keys = ["forum-%d" % d for d in found]
        with conn.cursor() as cur:
            cur.execute("SELECT event_key FROM calendar WHERE event_key = ANY(%s)", (keys,))
            known = {r[0] for r in cur.fetchall()}
        if forum_ids != cached:
            write_state(conn, "forum_ids",
                        json.dumps({str(k): list(v) for k, v in forum_ids.items()}))
    finally:
        conn.close()

    new = [(post, cid) for d, (post, cid) in found.items() if "forum-%d" % d not in known]
    new.sort(key=lambda pc: pc[0].get("created") or 0)

    if not baselined:
        # First ever run: record everything, send nothing. Only once at
        # least one forum was actually read -- otherwise there is no
        # baseline, just an empty run.
        if ok:
            conn = connect()
            try:
                upsert(conn, [calendar_row(p, courses[c]) for p, c in new])
                write_state(conn, "forum_baselined", datetime.now(IST).isoformat())
            finally:
                conn.close()
            log.info("forum baseline: recorded %d post(s), sent none", len(new))
        return 0

    cutoff = (datetime.now(IST) - timedelta(hours=FORUM_LOOKBACK_HOURS)).timestamp()

    def shown_at(post: dict) -> int:
        # A post can be scheduled to appear later than it was written.
        return max(post.get("created") or 0, post.get("timestart") or 0)

    fresh = [(p, c) for p, c in new if shown_at(p) >= cutoff]
    stale = [(p, c) for p, c in new if shown_at(p) < cutoff]

    if stale:
        conn = connect()
        try:
            upsert(conn, [calendar_row(p, courses[c]) for p, c in stale])
        finally:
            conn.close()
        log.info("recorded %d older post(s) without sending", len(stale))

    if not fresh:
        log.info("no new announcements")
        return 0

    log.info("%d new announcement(s)", len(fresh))

    # One LLM call for the batch. If it fails outright nothing is recorded,
    # so the whole batch is retried next run.
    batch = [
        {
            "subject": html.unescape(p.get("subject") or ""),
            "body": to_text(p.get("message"))
            + ("\n\nAttached files: " + ", ".join(n for n, _ in attachments(p))
               if attachments(p) else ""),
        }
        for p, _ in fresh
    ]
    items = summarise(batch)

    messages = []
    for i, (post, cid) in enumerate(fresh, 1):
        gid = post.get("groupid", EVERYONE)
        section = section_names.get(gid) if gid != EVERYONE else None
        cmid = (forum_ids.get(cid) or (0, 0))[1]
        messages.append((post, compose(post, items.get(i), courses[cid], section, cmid)))

    sent = send(messages)
    if sent:
        by_id = {p["discussion"]: c for p, c in fresh}
        conn = connect()
        try:
            upsert(conn, [calendar_row(p, courses[by_id[p["discussion"]]]) for p in sent])
        finally:
            conn.close()
    log.info("finished %d of %d announcement(s)", len(sent), len(fresh))
    return 0