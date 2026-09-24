#!/usr/bin/env python3
"""
Hourly echo notice bot. Runs as a Heroku Scheduler one-off dyno.

  1. Apply staged updates from stg_notice to src_notice, via an LLM diff.
  2. Purge expired notices from src_notice.
  3. Fire webhooks for notices due in the target hour.

current_affairs is a VIEW over src_notice, so it needs no refresh step.

Steps 2 and 3 always run, even when step 1 times out or every LLM provider
is down. Dispatch never depends on the LLM.

The run is split so that no Postgres connection is ever open during network
I/O. Neon bills compute time, and an idle connection held across a slow LLM
call is billed time spent waiting on someone else's API:

    connect -> read staged work -> DISCONNECT
    (LLM calls, validation -- no database)
    connect -> write updates, purge, select due, claim -> DISCONNECT
    (webhooks -- no database)

Two connections when there is staged work, one when there is not.

Dependencies: psycopg[binary], httpx. Nothing else -- no ORM, no SDK.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app_context import SUBJECT_CONTEXT, BOT_CONTEXT
from echo_bot import env_bool

IST = ZoneInfo("Asia/Kolkata")

DATABASE_URL = os.environ["DATABASE_URL"]

# Whatsmiau (Go server, Evolution-compatible routes). Names match the
# upstream bot's existing config so nothing has to be re-registered.
API_URL = os.environ["EVOLUTION_API_URL"].rstrip("/")
API_KEY = os.environ["EVOLUTION_API_KEY"]
INSTANCE = os.environ["EVOLUTION_INSTANCE"]

# 0  -> Scheduler runs hourly at :00, dispatch the hour that is starting.
# 45 -> Scheduler runs every 10 min, dispatch the next hour once past :45.
LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "0"))

CHUNK_SIZE = int(os.environ.get("LLM_CHUNK_SIZE", "15"))
LLM_DEADLINE_S = int(os.environ.get("LLM_DEADLINE_S", "240"))
LLM_TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "60"))
MAX_PENDING = int(os.environ.get("MAX_PENDING", "200"))

SEND_TIMEOUT_S = float(os.environ.get("SEND_TIMEOUT_S", "15"))
SENT_LOG_RETENTION_DAYS = int(os.environ.get("SENT_LOG_RETENTION_DAYS", "3"))

# Last-day boost: on a notice's final day, fire every N hours regardless of
# its own schedule, because that is the day it matters. Nothing is written
# to the database -- the rule is derived from end_date at dispatch time, so
# the user's stored schedule is never mutated and survives a later extend.
# Set LAST_DAY_BOOST=0 to remove it entirely; the query then reverts to
# exactly what it was before the feature existed.
LAST_DAY_BOOST = env_bool("LAST_DAY_BOOST", True)
LAST_DAY_EVERY_HOURS = int(os.environ.get("LAST_DAY_EVERY_HOURS", "2"))

# Quips: a one-line remark prepended to a reminder that fired because of the
# last-day boost. Cosmetic only. Everything about it is built to cost
# nothing on an ordinary run and to fail into silence, never into a missed
# reminder.
#
# QUIP_LLM_CHANCE is the probability of asking a model for a fresh line
# instead of using the static list. 0 disables the LLM path entirely.
QUIPS_ENABLED = env_bool("QUIPS_ENABLED", True)
QUIP_LLM_CHANCE = float(os.environ.get("QUIP_LLM_CHANCE", "0.3"))
QUIP_TIMEOUT_S = float(os.environ.get("QUIP_TIMEOUT_S", "8"))
QUIP_MAX_CHARS = int(os.environ.get("QUIP_MAX_CHARS", "120"))

LAST_DAY_START_HOUR = int(os.environ.get("LAST_DAY_START_HOUR", "8"))
LAST_DAY_END_HOUR = int(os.environ.get("LAST_DAY_END_HOUR", "22"))

# The Whatsmiau server runs on an Eco dyno and is probably asleep. The wake
# ping is fired before anything else so it boots while this bot does its
# database and LLM work; the health check then usually finds it already up.
HEALTH_WAIT_S = float(os.environ.get("HEALTH_WAIT_S", "45"))
WAKE_TIMEOUT_S = float(os.environ.get("WAKE_TIMEOUT_S", "5"))

# Neon suspends the compute when idle, so the first connection of a run
# pays a cold start. Generous timeout plus backoff, because a wake-up that
# takes a few seconds is normal, not an error.
CONNECT_TIMEOUT_S = int(os.environ.get("CONNECT_TIMEOUT_S", "30"))
CONNECT_ATTEMPTS = int(os.environ.get("CONNECT_ATTEMPTS", "4"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("echo_bot")

# httpx logs every request URL at INFO. Noise, and it puts the API host in
# Heroku's log drain. Warnings and above only.
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect() -> psycopg.Connection:
    """Open a connection, waiting out a Neon cold start."""
    delay = 1.0
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            conn = psycopg.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT_S)
            log.info("connected in %.2fs", time.monotonic() - started)
            return conn
        except psycopg.OperationalError as exc:
            if attempt == CONNECT_ATTEMPTS:
                raise
            log.warning(
                "connect attempt %d/%d failed (%s), retrying in %.0fs",
                attempt, CONNECT_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Subjects
#
# Same "CODE -> Name" block the upstream bot uses, supplied via env so the
# list can grow or shrink without a deploy. Unset means no subject_key
# validation -- better than rejecting every patch.
# ---------------------------------------------------------------------------



SUBJECT_CODES: set[str] = {"BITS_WILP"} | {
    line.split("->", 1)[0].strip().upper()
    for line in SUBJECT_CONTEXT.splitlines()
    if "->" in line
}


# ---------------------------------------------------------------------------
# LLM providers
#
# All speak the OpenAI /chat/completions shape, which is why this needs no
# provider abstraction library. Tried in order; skipped when the key is
# unset, abandoned on rate-limit or 5xx. Serial, one request at a time --
# free tiers cap requests per minute long before tokens per minute.
#
# Verify base URLs and model ids against provider docs before deploying;
# free-tier model names change often.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_env: str
    model: str
    json_mode: bool


PROVIDERS = (
    Provider(
        "groq",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
        json_mode=True,
    ),
    Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        json_mode=True,
    ),
    Provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        json_mode=False,
    ),
)

FAILOVER_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

SYSTEM_PROMPT = BOT_CONTEXT.replace('{subjects}', SUBJECT_CONTEXT)


def system_prompt() -> str:
    return SYSTEM_PROMPT.replace(
        "{subjects}", SUBJECT_CONTEXT or "(none supplied; leave subject_key alone)"
    )


def call_llm(payload: str, deadline: float) -> str:
    """POST to the first provider that answers. Raises when all fail."""
    last = "no provider configured"

    for p in PROVIDERS:
        if time.monotonic() > deadline:
            raise TimeoutError("deadline passed before provider " + p.name)

        key = os.environ.get(p.key_env)
        if not key:
            continue

        body = {
            "model": p.model,
            "temperature": 0,
            "max_tokens": 4000,
            "messages": [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": payload},
            ],
        }
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            r = httpx.post(
                p.base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
                timeout=LLM_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            last = f"{p.name}: {exc}"
            log.warning("provider %s transport error: %s", p.name, exc)
            continue

        if r.status_code in FAILOVER_STATUS:
            last = f"{p.name}: HTTP {r.status_code}"
            log.warning("provider %s returned %s, failing over", p.name, r.status_code)
            continue
        if r.status_code >= 400:
            last = f"{p.name}: HTTP {r.status_code} {r.text[:200]}"
            log.warning("provider %s rejected request: %s", p.name, last)
            continue

        log.info("provider %s answered", p.name)
        return r.json()["choices"][0]["message"]["content"]

    raise RuntimeError("all providers failed (" + last + ")")


# ---------------------------------------------------------------------------
# Patch validation
#
# The model proposes, this decides. Strict: any illegal field or malformed
# value voids the entire patch and the row is left for the next run. A model
# reaching for echo_title is misbehaving, so its other output is suspect --
# and message goes out verbatim over WhatsApp.
# ---------------------------------------------------------------------------

MUTABLE = frozenset({"subject_key", "message", "start_date", "end_date", "schedule"})
DAY_RULES = ("day_interval", "weekdays", "month_days")
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class Reject(Exception):
    """Patch failed validation; row is deferred."""


def parse_response(raw: str) -> dict[int, dict]:
    text = FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")

    out: dict[int, dict] = {}
    for item in json.loads(text[start : end + 1]).get("patches", []):
        try:
            out[int(item["id"])] = item.get("changes") or {}
        except (KeyError, TypeError, ValueError):
            log.warning("malformed patch item ignored: %r", item)
    return out


def _check_schedule(sched: object) -> dict:
    if not isinstance(sched, dict):
        raise Reject("schedule is not an object")

    hours = sched.get("hours")
    if not isinstance(hours, list) or not hours:
        raise Reject("schedule.hours missing or empty")
    if not all(isinstance(h, int) and not isinstance(h, bool) for h in hours):
        raise Reject("schedule.hours must be integers")
    if not all(0 <= h <= 23 for h in hours):
        raise Reject("schedule.hours out of range")
    if sorted(set(hours)) != hours:
        raise Reject("schedule.hours must be distinct and ascending")

    present = [k for k in DAY_RULES if k in sched]
    if len(present) != 1:
        raise Reject("schedule needs exactly one of " + ", ".join(DAY_RULES))
    rule = present[0]

    if set(sched) - {"hours", rule}:
        raise Reject("unexpected keys in schedule")

    val = sched[rule]
    if rule == "day_interval":
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise Reject("day_interval must be a positive integer")
    elif rule == "weekdays":
        if not isinstance(val, list) or not val:
            raise Reject("weekdays must be a non-empty list")
        if not all(isinstance(d, int) and 0 <= d <= 6 for d in val):
            raise Reject("weekdays out of range")
    else:
        if not isinstance(val, list) or not val:
            raise Reject("month_days must be a non-empty list")
        if not all(isinstance(d, int) and d != 0 and -31 <= d <= 31 for d in val):
            raise Reject("month_days out of range")

    return sched


def validate(changes: object, current: dict) -> dict:
    """Return the applicable changes, or {} for a no-op. Raises Reject."""
    if not isinstance(changes, dict):
        raise Reject("changes is not an object")

    illegal = set(changes) - MUTABLE
    if illegal:
        raise Reject("illegal field(s): " + ", ".join(sorted(illegal)))

    clean: dict = {}
    for field, val in changes.items():
        if field in ("start_date", "end_date"):
            if not isinstance(val, str):
                raise Reject(field + " must be a string")
            try:
                val = datetime.fromisoformat(val)
            except ValueError:
                raise Reject("unparseable " + field + ": " + val[:40])
            if val.tzinfo is None:
                raise Reject(field + " has no timezone offset")
            val = val.astimezone(IST)

        elif field == "schedule":
            val = _check_schedule(val)

        elif field == "subject_key":
            val = str(val).strip().upper()
            if SUBJECT_CODES and val not in SUBJECT_CODES:
                raise Reject("unknown subject_key " + val)

        else:  # message
            val = str(val).strip()
            if not val:
                raise Reject("message is empty")
            if "\n" in val:
                raise Reject("message must be one line")

        if val != current[field]:
            clean[field] = val

    if not clean:
        return {}

    start = clean.get("start_date", current["start_date"])
    end = clean.get("end_date", current["end_date"])
    if start > end:
        raise Reject("start_date after end_date")

    return clean


# ---------------------------------------------------------------------------
# Step 1 -- apply staged updates
# ---------------------------------------------------------------------------


def _payload(chunk: list[dict]) -> str:
    items = [
        {
            "id": i,
            "update_message": r["update_message"],
            "comments": r["comments"],
            "current": {
                "subject_key": r["subject_key"],
                "message": r["message"],
                "start_date": r["start_date"].astimezone(IST).isoformat(),
                "end_date": r["end_date"].astimezone(IST).isoformat(),
                "schedule": r["schedule"],
            },
        }
        for i, r in enumerate(chunk, 1)
    ]
    return json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))


def fetch_pending(conn: psycopg.Connection) -> list[dict]:
    """Clear the previous batch and read this run's staged work."""
    with conn.cursor() as cur:
        # Cleared at the start of a run rather than the end of the previous
        # one, so a failed run leaves its rows inspectable for an hour.
        cur.execute("DELETE FROM stg_notice WHERE processed")
        if cur.rowcount:
            log.info("cleared %d processed rows", cur.rowcount)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT g.target_group, g.echo_title, g.comments, g.update_message,
                   g.updated_by,
                   s.subject_key, s.message, s.start_date, s.end_date, s.schedule
            FROM stg_notice g
            JOIN src_notice s USING (target_group, echo_title)
            WHERE NOT g.processed
            ORDER BY g.target_group, g.echo_title
            LIMIT %s
            """,
            (MAX_PENDING,),
        )
        pending = cur.fetchall()
    conn.commit()
    return pending


Resolved = tuple[tuple[str, str], dict, str]


def resolve_updates(pending: list[dict]) -> list[Resolved]:
    """LLM round trips and validation. Runs with no database connection."""
    deadline = time.monotonic() + LLM_DEADLINE_S
    out: list[Resolved] = []

    for offset in range(0, len(pending), CHUNK_SIZE):
        if time.monotonic() > deadline:
            log.warning("deadline reached, deferring %d rows", len(pending) - offset)
            break

        chunk = pending[offset : offset + CHUNK_SIZE]
        try:
            patches = parse_response(call_llm(_payload(chunk), deadline))
        except Exception as exc:
            log.error("chunk %d failed, deferred: %s", offset // CHUNK_SIZE, exc)
            continue

        # ids are positional within this request only -- unique per call,
        # never stored. Keeps the identity fields out of the model's output.
        for i, row in enumerate(chunk, 1):
            key = (row["target_group"], row["echo_title"])
            try:
                changes = validate(patches.get(i, {}), row)
            except Reject as exc:
                log.warning("rejected patch for %s/%s: %s", *key, exc)
                continue
            out.append((key, changes, row["updated_by"] or "echo_bot"))

    return out


def write_updates(conn: psycopg.Connection, resolved: list[Resolved]) -> None:
    """Apply validated patches and mark their staged rows processed."""
    if not resolved:
        return

    with conn.cursor() as cur:
        for key, changes, updated_by in resolved:
            if not changes:
                log.info("no change for %s/%s", *key)
                continue
            fields = list(changes)
            cur.execute(
                "UPDATE src_notice SET "
                + ", ".join(f + " = %s" for f in fields)
                + ", last_updated_by = %s"
                + " WHERE target_group = %s AND echo_title = %s",
                [Jsonb(changes[f]) if f == "schedule" else changes[f] for f in fields]
                + [updated_by, *key],
            )
            log.info(
                "updated %s/%s %s",
                *key,
                json.dumps({k: str(v) for k, v in changes.items()}, ensure_ascii=False),
            )

        cur.execute(
            """
            UPDATE stg_notice SET processed = TRUE
            WHERE (target_group, echo_title) IN (
                SELECT * FROM unnest(%s::text[], %s::text[])
            )
            """,
            ([k[0] for k, _, _ in resolved], [k[1] for k, _, _ in resolved]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 2 -- purge expired
# ---------------------------------------------------------------------------


def purge_expired(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM src_notice WHERE end_date < now()")
        log.info("purged %d expired notices", cur.rowcount)
    conn.commit()


# ---------------------------------------------------------------------------
# Step 3 -- dispatch
#
# SQL narrows on the live window and the hours array; Python applies the day
# rule. Splitting it this way keeps the query trivially indexable and the
# calendar arithmetic in one readable place.
# ---------------------------------------------------------------------------


def target_slot(now: datetime) -> datetime:
    slot = now + timedelta(hours=1) if LEAD_MINUTES and now.minute >= LEAD_MINUTES else now
    return slot.replace(minute=0, second=0, microsecond=0)


def day_matches(schedule: dict, start: datetime, target: date) -> bool:
    if "day_interval" in schedule:
        delta = (target - start.astimezone(IST).date()).days
        return delta >= 0 and delta % schedule["day_interval"] == 0

    if "weekdays" in schedule:
        return target.weekday() in schedule["weekdays"]

    last = calendar.monthrange(target.year, target.month)[1]
    wanted = {d if d > 0 else last + 1 + d for d in schedule["month_days"]}
    return target.day in wanted


def is_due(row: dict, target_date: date, target_hour: int) -> tuple[bool, bool]:
    """Decide whether a candidate fires in this slot.

    Returns (due, by_boost). by_boost is True only when the last-day rule is
    what made it fire on an hour the notice would not otherwise have used --
    that is the case a quip marks.

    On a notice's final day the boost overrides the day rule entirely: the
    deadline is today, so an every-N-hours nudge is worth more than the
    original cadence. The notice's own scheduled hours still fire too.
    """
    hours = row["schedule"]["hours"]
    own_hour = target_hour in hours

    if LAST_DAY_BOOST and row["end_date"].astimezone(IST).date() == target_date:
        if own_hour:
            return True, False
        return target_hour % LAST_DAY_EVERY_HOURS == 0, True
        # for setting max and min active hours of reminders
        # return (
        #     target_hour in hours
        #     or (
        #         LAST_DAY_START_HOUR <= target_hour <= LAST_DAY_END_HOUR
        #         and target_hour % LAST_DAY_EVERY_HOURS == 0
        #     )
        # )

    # Not the last day: the hour must be one of the notice's own, and the
    # day rule must match. The hour check is already guaranteed by SQL when
    # the boost is off, and cheap enough to keep either way.
    return (
        own_hour
        and day_matches(row["schedule"], row["start_date"], target_date),
        False,
    )


def dispatch_where() -> str:
    """The hour condition, widened only when the boost is enabled."""
    if not LAST_DAY_BOOST:
        return "schedule->'hours' @> %(hour)s::jsonb"
    return (
        "(schedule->'hours' @> %(hour)s::jsonb"
        "\n                   OR (end_date AT TIME ZONE 'Asia/Kolkata')::date"
        " = %(today)s)"
    )


def select_and_claim(conn: psycopg.Connection) -> tuple[datetime, list[dict]]:
    """Find what is due this slot and claim it. Returns rows to send."""
    slot = target_slot(datetime.now(IST))
    target_date, target_hour = slot.date(), slot.hour
    log.info("dispatch slot %s %02d:00 IST", target_date, target_hour)

    with conn.cursor(row_factory=dict_row) as cur:
        params = {"slot": slot, "hour": json.dumps([target_hour])}
        if LAST_DAY_BOOST:
            params["today"] = target_date
        cur.execute(
            """
            SELECT target_group, echo_title, subject_key, message,
                   start_date, end_date, schedule
            FROM src_notice
            WHERE start_date <= %(slot)s
              AND end_date   >= %(slot)s
              AND """
            + dispatch_where(),
            params,
        )
        candidates = cur.fetchall()

    due = []
    for r in candidates:
        fires, by_boost = is_due(r, target_date, target_hour)
        if fires:
            r["by_boost"] = by_boost
            due.append(r)
    log.info("%d candidates, %d due after day rule", len(candidates), len(due))
    if not due:
        conn.commit()
        return slot, []

    # Claim the whole batch in one statement, before sending anything. The
    # PK conflict is the guard: a row already claimed for this slot is not
    # returned, so a duplicate or late run cannot resend it. Claiming first
    # means a crash mid-run skips a slot rather than double-sending.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sent_log (target_group, echo_title, target_date, target_hour)
            SELECT g, t, %s, %s
            FROM unnest(%s::text[], %s::text[]) AS u(g, t)
            ON CONFLICT DO NOTHING
            RETURNING target_group, echo_title
            """,
            (
                target_date,
                target_hour,
                [r["target_group"] for r in due],
                [r["echo_title"] for r in due],
            ),
        )
        claimed = set(cur.fetchall())

        cur.execute(
            "DELETE FROM sent_log WHERE sent_at < now() - make_interval(days => %s)",
            (SENT_LOG_RETENTION_DAYS,),
        )
    conn.commit()

    if len(due) - len(claimed):
        log.info("%d already sent for this slot", len(due) - len(claimed))

    return slot, [r for r in due if (r["target_group"], r["echo_title"]) in claimed]


def _jid_suffix(jid: str) -> str:
    """Last few digits only -- group JIDs are PII and never fully logged."""
    return jid.split("@")[0][-5:]


def wake_ping() -> None:
    """Nudge the Whatsmiau dyno awake. Fire and forget, failure is fine.

    Called before any other work so the dyno boots in parallel with the
    database read and the LLM calls, instead of us waiting on it later.
    """
    try:
        httpx.get(API_URL, headers={"apikey": API_KEY}, timeout=WAKE_TIMEOUT_S)
        log.info("wake ping sent")
    except httpx.HTTPError as exc:
        # A sleeping dyno often drops or times out the first request; the
        # ping still triggers the boot.
        log.info("wake ping did not complete (%s), boot likely triggered", type(exc).__name__)


def wait_healthy() -> bool:
    """Poll until the server returns 200, up to HEALTH_WAIT_S.

    GET on the API root -- that is the health route; sends are POSTs to
    /message/sendText under the same base. Only 200 counts as ready. An
    auth rejection is not a boot problem, so it aborts the wait immediately
    rather than burning the full window on a wrong key.
    """
    deadline = time.monotonic() + HEALTH_WAIT_S
    delay = 1.0

    while True:
        try:
            r = httpx.get(API_URL, headers={"apikey": API_KEY}, timeout=10.0)
            if r.status_code == 200:
                log.info("whatsmiau healthy")
                return True
            if r.status_code in (401, 403):
                log.error(
                    "whatsmiau rejected the api key (status=%s) -- not a boot "
                    "problem, check EVOLUTION_API_KEY", r.status_code,
                )
                return False
            reason = "status=%s" % r.status_code
        except httpx.HTTPError as exc:
            reason = type(exc).__name__

        if time.monotonic() + delay > deadline:
            log.error("whatsmiau not healthy after %.0fs (%s)", HEALTH_WAIT_S, reason)
            return False

        log.info("whatsmiau not ready (%s), retrying in %.0fs", reason, delay)
        time.sleep(delay)
        delay = min(delay * 2, 8.0)


def _static_quip(seed: str) -> str:
    """Pick a line deterministically. Imported lazily -- a run with no
    last-day sends never touches the module."""
    from quips import QUIPS

    if not QUIPS:
        return ""
    idx = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) % len(QUIPS)
    return QUIPS[idx]


def _llm_quip() -> str:
    """One short attempt at a fresh line. No failover, no retries.

    This is cosmetic, so it does not get to spend the budget the core
    update path relies on: first configured provider only, tight timeout,
    and any problem at all falls through to the static list.
    """
    for p in PROVIDERS:
        key = os.environ.get(p.key_env)
        if not key:
            continue
        r = httpx.post(
            p.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": p.model,
                "temperature": 1.0,
                "max_tokens": 60,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You write a single dry one-liner nudging Indian "
                            "MTech AI/ML students about a deadline that falls "
                            "today. Under 120 characters. Wry, never cruel. "
                            "No emoji, no quotes, no markdown. Output the line "
                            "only."
                        ),
                    },
                    {"role": "user", "content": "Write one."},
                ],
            },
            timeout=QUIP_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    return ""


def quip_for(seed: str) -> str:
    """A remark to prefix a boost-driven reminder, or "" for none.

    Never raises. Every failure path -- missing module, dead provider, bad
    output, bad config -- returns the empty string, and an empty string
    means the reminder goes out exactly as it would have anyway.
    """
    if not QUIPS_ENABLED:
        return ""

    try:
        if QUIP_LLM_CHANCE > 0 and random.random() < QUIP_LLM_CHANCE:
            try:
                text = _llm_quip().strip().strip('"')
                # Reject anything that would look wrong in a group chat.
                if text and len(text) <= QUIP_MAX_CHARS and text.count("\n") <= 1:
                    return text
                log.info("llm quip rejected, using static")
            except Exception as exc:
                log.info("llm quip unavailable (%s), using static", type(exc).__name__)

        return _static_quip(seed)
    except Exception:
        # Cosmetic feature. Never let it affect a reminder.
        log.warning("quip lookup failed, sending without one", exc_info=True)
        return ""


def send(slot: datetime, rows: list[dict]) -> None:
    """Send the reminders. Runs with no database connection.

    One client for the batch, so TCP and TLS are reused across sends.
    """
    if not rows:
        return

    sent = failed = 0
    print("API_URL",API_URL)
    # One quip per run, shared by every boost-driven reminder in this slot.
    # Computed once, and only when a boost row is actually going out.
    quip = ""
    if any(r.get("by_boost") for r in rows):
        quip = quip_for("%s|%02d" % (slot.date(), slot.hour))

    with httpx.Client(
        base_url=API_URL,
        headers={"apikey": API_KEY},
        timeout=SEND_TIMEOUT_S,
    ) as client:
        for row in rows:
            jid = row["target_group"]
            # Bold title as a header. It is the handle the user types to
            # update or remove the reminder, so it needs to be visible
            # without competing with the reminder itself.
            text = "*%s*\n%s" % (row["echo_title"], row["message"])
            if quip and row.get("by_boost"):
                text = "%s\n\n%s" % (quip, text)
            try:
                r = client.post(
                    "/message/sendText/" + INSTANCE,
                    json={"number": jid, "text": text},
                )
                if r.status_code >= 400:
                    # Status only -- never bodies, they carry PII and keys.
                    failed += 1
                    log.error(
                        "send failed: status=%s jid_suffix=%s echo_title=%s",
                        r.status_code, _jid_suffix(jid), row["echo_title"],
                    )
                else:
                    sent += 1
            except httpx.HTTPError as exc:
                # Claim stays. This slot is lost; the next scheduled hour
                # for this notice fires normally.
                failed += 1
                log.error(
                    "send error: %s jid_suffix=%s echo_title=%s",
                    type(exc).__name__, _jid_suffix(jid), row["echo_title"],
                )

    log.info("sent %d, failed %d", sent, failed)


# ---------------------------------------------------------------------------


def main() -> int:
    started = time.monotonic()
    db_time = 0.0

    # Wake the Whatsmiau dyno first, so it boots while we do everything
    # else. By the time we need it, the 10-30s Eco boot is usually done.
    wake_ping()

    # Phase 1: read staged work, then get off the database.
    t = time.monotonic()
    conn = connect()
    try:
        pending = fetch_pending(conn)
    finally:
        conn.close()
        db_time += time.monotonic() - t
    log.info("%d pending updates", len(pending))

    # Phase 2: LLM round trips and validation, no connection held.
    resolved: list[Resolved] = []
    if pending:
        try:
            resolved = resolve_updates(pending)
        except Exception:
            log.exception("update resolution failed, continuing to dispatch")

    # Phase 3: health check, still with no connection held. Done before
    # claiming so that a dead server means no claim -- otherwise sent_log
    # would record sends that never happened.
    healthy = wait_healthy()

    # Phase 4: all remaining database work on one connection.
    slot, to_send = target_slot(datetime.now(IST)), []
    t = time.monotonic()
    conn = connect()
    try:
        try:
            write_updates(conn, resolved)
        except Exception:
            conn.rollback()
            log.exception("update write failed, continuing")

        purge_expired(conn)

        if healthy:
            slot, to_send = select_and_claim(conn)
        else:
            log.error("skipping dispatch: whatsmiau unreachable")
    finally:
        conn.close()
        db_time += time.monotonic() - t

    # Phase 5: sends, no connection held.
    send(slot, to_send)

    total = time.monotonic() - started
    log.info("run finished in %.1fs (%.1fs connected)", total, db_time)
    return 0


if __name__ == "__main__":
    sys.exit(main())