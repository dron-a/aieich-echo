#!/usr/bin/env python3
"""
Mail bot. Runs from run.py, after echo_bot, on the same hourly schedule.

Part 1 (always on): read new mail under a Gmail label, summarise each one,
and post it to the group as a notification.

Part 2 (MAIL_TO_NOTICE=1): turn dated mails into reminders in src_notice.
Off by default -- part 1 stands on its own and part 2 depends on matching
mails to existing notices, which is the part worth releasing slowly.

Mail is read over IMAP with an app password. imaplib and email are stdlib,
so this adds no dependencies. If Google ever withdraws app passwords for
this account, only fetch_new_mail() has to change.

No dedup beyond the UID cursor: the same announcement sent twice as two
separate mails posts twice. UID tracking stops us re-reading a mail, not
the sender re-sending one.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from app_context import SUMMARY_PROMPT, SUBJECT_NAMES

import psycopg
from psycopg.rows import dict_row

from echo_bot import (
    API_KEY,
    API_URL,
    INSTANCE,
    IST,
    call_llm,
    connect,
    parse_response,
)

log = logging.getLogger("mail_bot")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "")
MAIL_TARGET_GROUP = os.environ.get("MAIL_TARGET_GROUP", "")

MAIL_ENABLED = os.environ.get("MAIL_ENABLED", "1") not in ("0", "false", "")
MAIL_LOOKBACK_DAYS = int(os.environ.get("MAIL_LOOKBACK_DAYS", "2"))
MAIL_MAX_PER_RUN = int(os.environ.get("MAIL_MAX_PER_RUN", "25"))
MAIL_BODY_CHARS = int(os.environ.get("MAIL_BODY_CHARS", "4000"))
MAIL_SUMMARY_CHARS = int(os.environ.get("MAIL_SUMMARY_CHARS", "900"))
MAIL_LLM_DEADLINE_S = int(os.environ.get("MAIL_LLM_DEADLINE_S", "90"))

# Course code as stored in src_notice.subject_key -- the AIML part only.
# The semester prefix is display-only and changes every term, so it is
# captured separately rather than baked into the key.
CODE_RE = re.compile(r"\b((?:S\d-\d{2})_)?(AIML[A-Z]*\d{3})\b")

# CODE -> "Course Name", parsed from the same env var echo_bot uses. Lets
# the header be built in Python instead of asked of the model.

KINDS = frozenset({"deadline", "event", "announcement"})
MARKUP = str.maketrans("", "", "*_~`")


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def read_cursor(conn: psycopg.Connection) -> tuple[int, int]:
    """(last_uid, uid_validity). Zeros when nothing has run yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM bot_state "
            "WHERE key IN ('mail_last_uid', 'mail_uid_validity')"
        )
        state = dict(cur.fetchall())
    conn.commit()
    return int(state.get("mail_last_uid", 0)), int(state.get("mail_uid_validity", 0))


def write_cursor(conn: psycopg.Connection, last_uid: int, uid_validity: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bot_state (key, value) VALUES
                ('mail_last_uid', %s), ('mail_uid_validity', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (str(last_uid), str(uid_validity)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _decode(raw: str | None) -> str:
    """MIME-decode a header into plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _body(msg: email.message.Message) -> str:
    """Plain-text body, HTML ignored. Truncated -- the top of a mail
    carries the dates, and long threads are mostly quoted history."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    parts.append(
                        part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    )
                except Exception:
                    continue
    else:
        try:
            parts.append(
                msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
            )
        except Exception:
            pass

    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAIL_BODY_CHARS]


def subject_key_for(subject: str, body: str) -> tuple[str, str]:
    """(subject_key, display_prefix) for a mail.

    The subject line wins. The body is a fallback because the code is not
    always in the subject -- but only when it names exactly one course:
    a body mentioning two is ambiguous, and guessing wrong files the notice
    under the wrong course.
    """
    match = CODE_RE.search(subject)
    if not match:
        found = {m.group(2).upper() for m in CODE_RE.finditer(body)}
        if len(found) != 1:
            return "BITS_WILP", "BITS_WILP"
        match = CODE_RE.search(body)

    code = match.group(2).upper()
    prefix = (match.group(1) or "") + code
    name = SUBJECT_NAMES.get(code)
    return code, f"{prefix} {name}" if name else prefix


def fetch_new_mail(last_uid: int, uid_validity: int) -> tuple[list[dict], int, int]:
    """New mail under the label, oldest first. Returns (mails, max_uid, validity)."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        status, _ = imap.select(f'"{GMAIL_LABEL}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot select label {GMAIL_LABEL!r}")

        status, data = imap.status(f'"{GMAIL_LABEL}"', "(UIDVALIDITY)")
        current_validity = int(re.search(rb"UIDVALIDITY (\d+)", data[0]).group(1))

        if current_validity != uid_validity:
            # Folder was reset: stored UIDs mean nothing now. Fall back to a
            # date window so a lost cursor cannot replay months of backlog.
            if uid_validity:
                log.warning("UIDVALIDITY changed, falling back to date search")
            since = (datetime.now(IST) - timedelta(days=MAIL_LOOKBACK_DAYS)).strftime(
                "%d-%b-%Y"
            )
            status, data = imap.uid("SEARCH", None, f"(SINCE {since})")
        else:
            status, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")

        uids = [int(u) for u in data[0].split()] if status == "OK" and data[0] else []
        # "UID n:*" always returns at least the highest UID, even when it is
        # below n -- IMAP treats * as "highest" and flips the range.
        uids = sorted(u for u in uids if u > last_uid or current_validity != uid_validity)
        if not uids:
            return [], last_uid, current_validity

        if len(uids) > MAIL_MAX_PER_RUN:
            log.warning("%d new mails, capping at %d", len(uids), MAIL_MAX_PER_RUN)
            uids = uids[:MAIL_MAX_PER_RUN]

        mails = []
        for uid in uids:
            status, data = imap.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK" or not data or not data[0]:
                log.warning("could not fetch uid %d, skipping", uid)
                continue
            msg = email.message_from_bytes(data[0][1])
            subject = _decode(msg.get("Subject"))
            body = _body(msg)
            key, prefix = subject_key_for(subject, body)
            mails.append(
                {
                    "uid": uid,
                    "subject": subject,
                    "body": body,
                    "subject_key": key,
                    "prefix": prefix,
                }
            )

        return mails, max(uids), current_validity
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Summarise
# ---------------------------------------------------------------------------


def summarise(mails: list[dict]) -> dict[int, dict]:
    """One call for the whole batch. Returns {id: item} for valid items."""
    payload = json.dumps(
        {
            "items": [
                {"id": i, "subject": m["subject"], "body": m["body"]}
                for i, m in enumerate(mails, 1)
            ]
        },
        ensure_ascii=False,
    )

    import time

    raw = call_llm(
        payload,
        time.monotonic() + MAIL_LLM_DEADLINE_S,
        system_prompt=SUMMARY_PROMPT,
    )
    parsed = parse_response(raw, key="items")

    out: dict[int, dict] = {}
    for item_id, item in parsed.items():
        kind = str(item.get("kind", "")).strip().lower()
        topic = str(item.get("topic", "")).strip().translate(MARKUP)
        summary = str(item.get("summary", "")).strip().translate(MARKUP)

        if kind not in KINDS or not topic or not summary:
            log.warning("dropping malformed item %s", item_id)
            continue

        if len(summary) > MAIL_SUMMARY_CHARS:
            # Cut at a line boundary where possible -- a partial summary
            # still beats silence, and the mail is in their inbox anyway.
            cut = summary[:MAIL_SUMMARY_CHARS]
            nl = cut.rfind("\n")
            summary = (cut[:nl] if nl > MAIL_SUMMARY_CHARS // 2 else cut).rstrip() + "…"

        out[item_id] = {"kind": kind, "topic": topic, "summary": summary}

    return out


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------


def calendar_rows(mails: list[dict], items: dict[int, dict]) -> list[dict]:
    """Announcements only, for the log.

    start_date and end_date are both the moment the mail arrived: an
    announcement has no period, and dating it to now means it is already in
    the past by the next run, so the sync step's "end_date > now()" filter
    can never pick it up. event_type excludes it as well -- two independent
    reasons, because a notification-only item becoming a reminder would be
    a visible bug.
    """
    now = datetime.now(IST)
    rows = []
    for i, mail in enumerate(mails, 1):
        item = items.get(i)
        if not item or item["kind"] != "announcement":
            continue
        rows.append(
            {
                "event_key": "mail-%s" % uuid.uuid4(),
                "source": "mail",
                "event_type": "announcement",
                "subject_key": mail["subject_key"],
                "title": item["topic"][:200],
                "message": item["summary"],
                "start_date": now,
                "end_date": now,
            }
        )
    return rows


def notify(mails: list[dict], items: dict[int, dict]) -> int:
    """Post one message per summarised mail. Returns the count sent."""
    import httpx

    sent = 0
    with httpx.Client(
        base_url=API_URL, headers={"apikey": API_KEY}, timeout=15.0
    ) as client:
        for i, mail in enumerate(mails, 1):
            item = items.get(i)
            if not item:
                continue

            text = "*%s*\n%s\n%s" % (mail["prefix"], item["topic"], item["summary"])
            try:
                r = client.post(
                    "/message/sendText/" + INSTANCE,
                    json={"number": MAIL_TARGET_GROUP, "text": text},
                )
                if r.status_code >= 400:
                    log.error("notify failed: status=%s uid=%d", r.status_code, mail["uid"])
                else:
                    sent += 1
            except Exception as exc:
                log.error("notify error: %s uid=%d", type(exc).__name__, mail["uid"])

    return sent


# ---------------------------------------------------------------------------


def main() -> int:
    if not MAIL_ENABLED:
        log.info("mail bot disabled")
        return 0

    missing = [
        n
        for n, v in (
            ("GMAIL_ADDRESS", GMAIL_ADDRESS),
            ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD),
            ("GMAIL_LABEL", GMAIL_LABEL),
            ("MAIL_TARGET_GROUP", MAIL_TARGET_GROUP),
        )
        if not v
    ]
    if missing:
        log.error("mail bot not configured, missing: %s", ", ".join(missing))
        return 0

    conn = connect()
    try:
        last_uid, uid_validity = read_cursor(conn)
    finally:
        conn.close()

    # IMAP and the LLM both run with no database connection held.
    mails, max_uid, validity = fetch_new_mail(last_uid, uid_validity)
    if not mails:
        log.info("no new mail")
        if validity != uid_validity:
            conn = connect()
            try:
                write_cursor(conn, max_uid, validity)
            finally:
                conn.close()
        return 0

    log.info("%d new mail(s)", len(mails))
    items = summarise(mails)
    sent = notify(mails, items)
    log.info("notified %d of %d", sent, len(mails))

    rows = calendar_rows(mails, items)
    conn = connect()
    try:
        if rows:
            import cal_bot

            cal_bot.upsert(conn, rows)
            log.info("logged %d announcement(s) to calendar", len(rows))
        write_cursor(conn, max_uid, validity)
    finally:
        conn.close()

    return 0