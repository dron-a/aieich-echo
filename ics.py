#!/usr/bin/env python3
"""
Minimal iCalendar reader for Microsoft Exchange published calendars.

Handles what those feeds actually contain, and nothing more:
  - line unfolding (continuations begin with a space or tab)
  - VEVENT extraction with parameterised properties (DTSTART;TZID=...)
  - RRULE expansion via dateutil, so any FREQ works, not just WEEKLY
  - RECURRENCE-ID overrides, which move or replace a single occurrence
  - EXDATE exclusions
  - the Windows timezone name Exchange emits ("India Standard Time"),
    which zoneinfo cannot resolve

Everything is IST: the feed's own VTIMEZONE declares +05:30 for both
standard and daylight, so no DST handling is required.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

IST = ZoneInfo("Asia/Kolkata")

# Exchange writes Windows timezone names. Only the ones plausible for this
# feed are mapped; anything else falls back to IST rather than raising,
# because a wrong-by-an-offset event still beats a dropped one.
WINDOWS_TZ = {
    "India Standard Time": IST,
}

# "Course Name (S1-26_AIMLZG521) - Webinar" / "...(S1-26_AIMLZG521)- Webinar"
WEBINAR_RE = re.compile(r"-\s*webinar\s*$", re.IGNORECASE)
CODE_RE = re.compile(r"\b((?:S\d-\d{2})_)?(AIML[A-Z]*\d{3})\b")


def unfold(text: str) -> list[str]:
    """Join RFC 5545 continuation lines. A line beginning with a space or
    tab continues the previous one, with that whitespace removed."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    """"DTSTART;TZID=India Standard Time:20260830T133000" ->
    ("DTSTART", {"TZID": "India Standard Time"}, "20260830T133000")"""
    colon = line.find(":")
    if colon == -1:
        return None

    head, value = line[:colon], line[colon + 1 :]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_dt(value: str, params: dict[str, str]) -> datetime | None:
    """ICS datetime -> aware datetime. Handles the trailing-Z UTC form, the
    TZID form, and bare dates (all-day events)."""
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=ZoneInfo("UTC")
            )
        if "T" in value:
            naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
        else:
            naive = datetime.strptime(value, "%Y%m%d")
        tz = WINDOWS_TZ.get(params.get("TZID", ""), IST)
        return naive.replace(tzinfo=tz)
    except ValueError:
        return None


def split_events(text: str) -> list[dict]:
    """Raw VEVENT blocks as {PROPERTY: (params, value)}. DTSTART and DTEND
    keep their parameters because TZID lives there."""
    events, current = [], None
    for line in unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
        elif line.startswith("END:VEVENT"):
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            prop = parse_prop(line)
            if prop:
                name, params, value = prop
                # EXDATE can repeat; everything else takes the last value.
                if name == "EXDATE" and name in current:
                    current[name] = (params, current[name][1] + "," + value)
                else:
                    current[name] = (params, value)
    return events


def expand(
    raw: list[dict], window_start: datetime, window_end: datetime
) -> list[dict]:
    """Occurrences within the window, overrides applied.

    A recurring VEVENT yields one entry per occurrence. A VEVENT carrying
    RECURRENCE-ID replaces the occurrence it names -- Exchange uses this to
    move a single class, so ignoring it would produce a session that did not
    happen and miss the one that did.
    """
    masters, overrides = [], {}

    for ev in raw:
        uid = ev.get("UID", ({}, ""))[1]
        if not uid:
            continue
        if "RECURRENCE-ID" in ev:
            params, value = ev["RECURRENCE-ID"]
            original = parse_dt(value, params)
            if original:
                overrides[(uid, original)] = ev
        else:
            masters.append(ev)

    out: list[dict] = []
    seen: set[tuple[str, datetime]] = set()

    for ev in masters:
        uid = ev["UID"][1]
        start = parse_dt(*reversed(ev.get("DTSTART", ({}, ""))))
        if not start:
            continue
        end = parse_dt(*reversed(ev.get("DTEND", ({}, "")))) or start
        duration = end - start

        if "RRULE" in ev:
            starts = _occurrences(ev, start, window_start, window_end)
        else:
            starts = [start]

        for occ in starts:
            override = overrides.pop((uid, occ), None)
            if override:
                o_start = parse_dt(*reversed(override.get("DTSTART", ({}, ""))))
                if not o_start:
                    continue
                o_end = parse_dt(*reversed(override.get("DTEND", ({}, "")))) or (
                    o_start + duration
                )
                occ_start, occ_end, summary = (
                    o_start,
                    o_end,
                    override.get("SUMMARY", ({}, ""))[1],
                )
            else:
                occ_start, occ_end = occ, occ + duration
                summary = ev.get("SUMMARY", ({}, ""))[1]

            if not (window_start <= occ_start <= window_end):
                continue
            if (uid, occ_start) in seen:
                continue
            seen.add((uid, occ_start))
            out.append(
                {
                    "uid": uid,
                    "start": occ_start,
                    "end": occ_end,
                    "summary": summary.strip(),
                }
            )

    # An override whose original occurrence fell outside the window, or whose
    # master is missing, is still a real event.
    for (uid, _), ev in overrides.items():
        start = parse_dt(*reversed(ev.get("DTSTART", ({}, ""))))
        if not start or not (window_start <= start <= window_end):
            continue
        if (uid, start) in seen:
            continue
        end = parse_dt(*reversed(ev.get("DTEND", ({}, "")))) or start
        out.append(
            {
                "uid": uid,
                "start": start,
                "end": end,
                "summary": ev.get("SUMMARY", ({}, ""))[1].strip(),
            }
        )

    return sorted(out, key=lambda e: e["start"])


def _occurrences(
    ev: dict, start: datetime, window_start: datetime, window_end: datetime
) -> list[datetime]:
    """Expand RRULE within the window. dateutil handles every FREQ, so a
    monthly or daily rule appearing next term needs no code change.

    UNTIL is often UTC while DTSTART is local -- dateutil compares them
    correctly, which is the main reason this is not hand-rolled.
    """
    try:
        rule = rrulestr(ev["RRULE"][1], dtstart=start)
    except Exception:
        return [start]

    excluded = set()
    if "EXDATE" in ev:
        params, value = ev["EXDATE"]
        for piece in value.split(","):
            dt = parse_dt(piece, params)
            if dt:
                excluded.add(dt)

    # between() is inclusive; pad by a day so an occurrence exactly on the
    # boundary is not lost to rounding.
    try:
        occurrences = rule.between(
            window_start - timedelta(days=1), window_end + timedelta(days=1), inc=True
        )
    except Exception:
        return [start]

    return [o for o in occurrences if o not in excluded]


def classify(summary: str) -> tuple[str, str]:
    """(event_type, subject_key) from the SUMMARY line.

    Exchange writes "Course Name (S1-26_AIMLZG521) - Webinar" for webinars
    and "... - Prof. Name" for classes, so this is a regex rather than a
    model call.
    """
    match = CODE_RE.search(summary)
    subject_key = match.group(2).upper() if match else "BITS_WILP"
    return ("webinar" if WEBINAR_RE.search(summary) else "class"), subject_key


def parse_calendar(
    text: str, window_start: datetime, window_end: datetime
) -> list[dict]:
    """ICS text -> occurrences in the window, classified."""
    out = []
    for occ in expand(split_events(text), window_start, window_end):
        event_type, subject_key = classify(occ["summary"])
        out.append({**occ, "event_type": event_type, "subject_key": subject_key})
    return out
