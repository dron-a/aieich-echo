#!/usr/bin/env python3
"""
Hourly echo notice bot. Runs as a Heroku Scheduler one-off dyno.

  1. Apply staged updates from stg_notice to src_notice, via an LLM diff.
  2. Purge expired notices from src_notice.
  3. Fire webhooks for notices due in the target hour.

current_reminders is a VIEW over src_notice, so it needs no refresh step.

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

def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

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

# Phones allowed to update any reminder, not only their own. Must match the
# format the upstream bot stores in created_by/updated_by exactly -- this is
# a string comparison, so "919876543210" and "+91 98765 43210" differ.
ADMIN_PHONES = [
    p.strip() for p in os.environ.get("ADMIN_PHONES", "").split(",") if p.strip()
]
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
# Deadline boost: as a notice's end_date approaches, fire extra reminders on
# top of its own schedule, tightening the interval as the deadline nears.
# Nothing is written to the database -- the rule is derived from end_date at
# dispatch time, so the user's stored schedule is never mutated.
#
# BOOST_BANDS is "threshold:interval" pairs, widest first: with the default,
# 12+ hours out fires every 6h, 4-11 hours every 3h, under 4 hours hourly.
# Intervals are counted back from end_date, not from midnight, so the hour
# of the deadline itself always fires.
#
# BOOST_KINDS is which notice_kind values may boost. NULL counts as unknown.
# Set BOOST_ENABLED=0 to remove it; the query reverts to what it was before
# the feature existed.
BOOST_ENABLED = env_bool("BOOST_ENABLED", True)
BOOST_KINDS = frozenset(
    k.strip().lower()
    for k in os.environ.get("BOOST_KINDS", "deadline,unknown").split(",")
    if k.strip()
)


def _parse_bands(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse "12:6,4:3,0:1" into descending (threshold, interval) pairs.

    Falls back to the default on anything malformed. A cosmetic feature's
    config must never be able to take down dispatch.
    """
    try:
        bands = []
        for pair in raw.split(","):
            threshold, interval = pair.split(":")
            threshold, interval = int(threshold), int(interval)
            if threshold < 0 or interval < 1:
                raise ValueError("threshold must be >= 0 and interval >= 1")
            bands.append((threshold, interval))
        if not bands:
            raise ValueError("no bands")
        return tuple(sorted(bands, reverse=True))
    except Exception:
        log.warning("BOOST_BANDS %r is malformed, using defaults", raw)
        return ((12, 6), (4, 3), (0, 1))


BOOST_BANDS_RAW = os.environ.get("BOOST_BANDS", "12:6,4:3,0:1")

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

# Parsed here rather than beside the other config because _parse_bands logs
# on malformed input, and the logger does not exist until now.
BOOST_BANDS = _parse_bands(BOOST_BANDS_RAW)

# Rows ending beyond the widest band can never boost, so they are not worth
# fetching. One hour of slack because remaining time is floored to whole
# hours: a deadline 12.9h out floors to 12, still inside the widest band.
BOOST_HORIZON_H = BOOST_BANDS[0][0] + 1

MENTIONS_EVERY_ONE = env_bool("MENTIONS_EVERY_ONE", False)


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
        "gemini",
        os.environ.get("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "GEMINI_API_KEY",
        os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        json_mode=True,
    ),
    Provider(
        "groq",
        os.environ.get("GROQ_URL", "https://api.groq.com/openai/v1"),
        "GROQ_API_KEY",
        os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
        json_mode=True,
    ),
    Provider(
        "openrouter",
        os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1"),
        "OPENROUTER_API_KEY",
        os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        json_mode=False,
    ),
)

FAILOVER_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# SYSTEM_PROMPT = BOT_CONTEXT.replace('{subjects}', SUBJECT_CONTEXT)
SYSTEM_PROMPT = BOT_CONTEXT


def patch_prompt() -> str:
    """The reminder-update system prompt, with the subject list injected."""
    return SYSTEM_PROMPT.replace(
        "{subjects}", SUBJECT_CONTEXT or "(none supplied; leave subject_key alone)"
    )


def call_llm(payload: str, deadline: float, system_prompt: str | None = None) -> str:
    """POST to the first provider that answers. Raises when all fail.

    system_prompt defaults to the reminder-patch prompt built below; the
    mail bot passes its own.
    """
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
                {"role": "system", "content": system_prompt or patch_prompt()},
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


def parse_response(raw: str, key: str = "patches") -> dict[int, dict]:
    """Extract {id: payload} from a model response.

    key selects the list to read and what each item carries: "patches" holds
    a "changes" object per id, any other key holds the item itself. The mail
    bot uses "items"; the update path uses the default.
    """
    text = FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")

    out: dict[int, dict] = {}
    for item in json.loads(text[start : end + 1]).get(key, []):
        try:
            out[int(item["id"])] = (
                (item.get("changes") or {}) if key == "patches" else item
            )
        except (KeyError, TypeError, ValueError):
            log.warning("malformed item ignored: %r", item)
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
    """Clear the previous batch and read this run's staged work.

    Who may update what is decided by the join:
      - the owner, on their own reminder
      - anyone, on a calendar-synced reminder (created_by 'echo_cal')
      - an admin, on anything

    Each row carries two people. owner is the reminder's created_by and is
    what the UPDATE targets; stager is who asked for the change and is what
    last_updated_by records. For an owner editing their own reminder they are
    the same; for an admin or a human editing a bot reminder they are not,
    and conflating them makes the UPDATE match nothing.
    """
    with conn.cursor() as cur:
        # Cleared at the start of a run rather than the end of the previous
        # one, so a failed run leaves its rows inspectable for an hour.
        cur.execute("DELETE FROM stg_notice WHERE processed")
        if cur.rowcount:
            log.info("cleared %d processed rows", cur.rowcount)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT g.target_group, g.echo_title,
                   g.updated_by AS stager, s.created_by AS owner,
                   g.comments, g.update_message,
                   s.subject_key, s.message, s.start_date, s.end_date, s.schedule
            FROM stg_notice g
            JOIN src_notice s
              ON s.target_group = g.target_group
             AND s.echo_title   = g.echo_title
             AND (s.created_by = g.updated_by
                  OR s.created_by = 'echo_cal'
                  OR g.updated_by = ANY(%(admins)s))
            WHERE NOT g.processed
            ORDER BY g.target_group, g.echo_title, g.updated_by
            LIMIT %(limit)s
            """,
            {"admins": ADMIN_PHONES, "limit": MAX_PENDING},
        )
        rows = cur.fetchall()
    conn.commit()
    return resolve_targets(rows)


def resolve_targets(rows: list[dict]) -> list[dict]:
    """One target reminder per staged update, or none.

    The join can match more than one reminder for a single staged row: an
    admin asking to update mlops-1 in a group where two people each have
    one. Applying the change to both is almost certainly wrong, so:

      - if the stager owns one of the matches, that is the one they meant
      - otherwise, if there is exactly one match, use it
      - otherwise it is ambiguous and is skipped, logged, and marked done so
        it does not retry every hour

    The upstream bot is the right place to resolve this -- it can ask the
    admin whose reminder they mean. This is the backstop if it does not.
    """
    by_stage: dict[tuple, list[dict]] = {}
    for r in rows:
        by_stage.setdefault((r["target_group"], r["echo_title"], r["stager"]), []).append(r)

    out = []
    for key, matches in by_stage.items():
        own = [m for m in matches if m["owner"] == m["stager"]]
        if own:
            out.append(own[0])
        elif len(matches) == 1:
            out.append(matches[0])
        else:
            log.warning(
                "ambiguous update for %s/%s by %s: %d reminders share that title",
                key[0], key[1], key[2][-4:], len(matches),
            )
            # Flagged rather than dropped, so write_updates marks it processed.
            out.append({**matches[0], "ambiguous": True})
    return out


# ((target_group, echo_title, owner), stager, changes). owner identifies the
# reminder -- it is part of src_notice's key. stager is who asked, and is
# what stg_notice is keyed on and what last_updated_by records. The two
# differ when an admin edits someone else's reminder or a human edits a bot
# one. changes is None for a staged row that cannot be applied (ambiguous)
# but must still be marked processed.
Resolved = tuple[tuple[str, str, str], str, dict | None]


def resolve_updates(pending: list[dict]) -> list[Resolved]:
    """LLM round trips and validation. Runs with no database connection."""
    deadline = time.monotonic() + LLM_DEADLINE_S
    out: list[Resolved] = []

    # Ambiguous rows never reach the model -- there is no single reminder to
    # patch. They go straight through as unapplied so they are still marked
    # processed and do not retry every hour.
    for row in [r for r in pending if r.get("ambiguous")]:
        out.append(((row["target_group"], row["echo_title"], row["owner"]),
                    row["stager"], None))
    pending = [r for r in pending if not r.get("ambiguous")]

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
            key = (row["target_group"], row["echo_title"], row["owner"])
            try:
                changes = validate(patches.get(i, {}), row)
            except Reject as exc:
                log.warning("rejected patch for %s/%s: %s", key[0], key[1], exc)
                continue
            out.append((key, row["stager"], changes))

    return out


def write_updates(conn: psycopg.Connection, resolved: list[Resolved]) -> None:
    """Apply validated patches and mark their staged rows processed."""
    if not resolved:
        return

    with conn.cursor() as cur:
        for key, stager, changes in resolved:
            if not changes:
                if changes is None:
                    log.info("skipped ambiguous %s/%s", key[0], key[1])
                else:
                    log.info("no change for %s/%s", key[0], key[1])
                continue
            fields = list(changes)
            cur.execute(
                "UPDATE src_notice SET "
                + ", ".join(f + " = %s" for f in fields)
                + ", last_updated_by = %s"
                + " WHERE target_group = %s AND echo_title = %s"
                + "   AND created_by = %s",
                [Jsonb(changes[f]) if f == "schedule" else changes[f] for f in fields]
                # last_updated_by is the stager; created_by in the WHERE is the
                # owner. For a human editing a bot reminder this is also what
                # makes the calendar sync stop overwriting it -- the sync only
                # touches rows still last updated by echo_cal.
                + [stager, *key],
            )
            log.info(
                "updated %s/%s %s",
                key[0], key[1],
                json.dumps({k: str(v) for k, v in changes.items()}, ensure_ascii=False),
            )

        cur.execute(
            """
            UPDATE stg_notice SET processed = TRUE
            WHERE (target_group, echo_title, updated_by) IN (
                SELECT * FROM unnest(%s::text[], %s::text[], %s::text[])
            )
            """,
            # Marked by stager, not owner: stg_notice is keyed on who staged.
            (
                [k[0] for k, _, _ in resolved],
                [k[1] for k, _, _ in resolved],
                [stager for _, stager, _ in resolved],
            ),
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


def band_for(hours_left: int) -> int | None:
    """Boost interval for this much time remaining, or None if too far out.

    The widest band is bounded by the horizon: without that, hours_left >=
    the widest threshold is always true and a deadline days away would
    boost forever. SQL already filters those rows out, but the two layers
    must agree -- this is what makes the function correct on its own.
    """
    if hours_left < 0 or hours_left >= BOOST_HORIZON_H:
        return None
    for threshold, interval in BOOST_BANDS:
        if hours_left >= threshold:
            return interval
    return None


def is_due(row: dict, slot: datetime, target_date: date) -> tuple[bool, bool]:
    """Decide whether a candidate fires in this slot.

    Returns (due, by_boost). by_boost is True only when the deadline boost
    is what made it fire on an hour the notice would not otherwise have
    used -- that is the case a quip marks.

    Own scheduled hours always win and never carry a quip. The boost fires
    on top of them as end_date approaches, on intervals counted back from
    end_date so the deadline hour itself always lands.
    """
    if slot.hour in row["schedule"]["hours"]:
        return day_matches(row["schedule"], row["start_date"], target_date), False

    if BOOST_ENABLED and (row["notice_kind"] or "unknown").strip().lower() in BOOST_KINDS:
        # Floored to whole hours: a deadline at 23:59 reads as 0 hours left
        # at the 23:00 slot, which is what makes that final nudge fire.
        hours_left = int((row["end_date"] - slot).total_seconds() // 3600)
        interval = band_for(hours_left)
        if interval and hours_left % interval == 0:
            return True, True

    return False, False


def dispatch_where() -> str:
    """The hour condition, widened only when the boost is enabled."""
    if not BOOST_ENABLED:
        return "schedule->'hours' @> %(hour)s::jsonb"
    return (
        "(schedule->'hours' @> %(hour)s::jsonb"
        "\n                   OR end_date < %(boost_horizon)s)"
    )


def select_and_claim(conn: psycopg.Connection) -> tuple[datetime, list[dict]]:
    """Find what is due this slot and claim it. Returns rows to send."""
    slot = target_slot(datetime.now(IST))
    target_date, target_hour = slot.date(), slot.hour
    log.info("dispatch slot %s %02d:00 IST", target_date, target_hour)

    with conn.cursor(row_factory=dict_row) as cur:
        params = {"slot": slot, "hour": json.dumps([target_hour])}
        if BOOST_ENABLED:
            params["boost_horizon"] = slot + timedelta(hours=BOOST_HORIZON_H)
        cur.execute(
            """
            SELECT target_group, echo_title, created_by, subject_key, message,
                   start_date, end_date, schedule, notice_kind
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
        fires, by_boost = is_due(r, slot, target_date)
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
    #
    # created_by is part of the claim because it is part of the notice's
    # identity: two people in one group may each hold a reminder called
    # mlops-1, and one claiming the slot must not suppress the other.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sent_log (target_group, echo_title, created_by,
                                  target_date, target_hour)
            SELECT g, t, c, %s, %s
            FROM unnest(%s::text[], %s::text[], %s::text[]) AS u(g, t, c)
            ON CONFLICT DO NOTHING
            RETURNING target_group, echo_title, created_by
            """,
            (
                target_date,
                target_hour,
                [r["target_group"] for r in due],
                [r["echo_title"] for r in due],
                [r["created_by"] for r in due],
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

    return slot, [
        r
        for r in due
        if (r["target_group"], r["echo_title"], r["created_by"]) in claimed
    ]


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
            requestor = "all"
            # Bold title as a header. It is the handle the user types to
            # update or remove the reminder, so it needs to be visible
            # without competing with the reminder itself.
            text = "*%s*\n%s" % (row["echo_title"], row["message"])
            if quip and row.get("by_boost"):
                text = "%s\n\n%s" % (quip, text)
            if MENTIONS_EVERY_ONE:
                if row["created_by"] != 'echo_cal':
                    requestor = str(row["created_by"])
                text = "%s\n\n%s" % (f"@{requestor}", text)
            try:
                r = client.post(
                    "/message/sendText/" + INSTANCE,
                    json={"number": jid, "text": text, "mentioned":[requestor], "mentionsEveryOne": MENTIONS_EVERY_ONE},
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