#!/usr/bin/env python3
"""
Scheduler entrypoint. Runs every stage in one dyno, in order.

  echo_bot     reminders -- time-critical, runs first and always
  mail_bot     new mail notifications
  course_bot   enrolment sync (monthly)
  cal_bot      Taxila and Teams calendar sync
  content_bot  course content sync (daily)
  group_bot    group membership sync (daily)
  forum_bot    course announcements

Each stage is isolated: one failing never stops the rest, and imports are
lazy so a broken module cannot stop the stages before it. Order follows
dependencies -- course_bot fills dim_taxila_course, which content_bot,
group_bot and forum_bot read.

Every stage logs a start line and a done line with its own time, and every
log line is tagged with the stage that wrote it.
"""

import logging
import os
import sys
import time

STAGES = [
    os.environ.get("MAIL_MODULE", "mail_bot"),
    os.environ.get("COURSE_MODULE", "course_bot"),
    os.environ.get("CALENDAR_MODULE", "cal_bot_monthly"),
    os.environ.get("CONTENT_MODULE", "content_bot"),
    os.environ.get("GROUP_MODULE", "group_bot"),
    os.environ.get("FORUM_MODULE", "forum_bot"),
]


def run_stage(name: str, set_stage) -> None:
    log = logging.getLogger(name)
    set_stage(name)
    log.info("▶ started")
    started = time.monotonic()
    try:
        __import__(name).main()
        log.info("■ done in %.1fs", time.monotonic() - started)
    except Exception:
        log.exception("■ failed after %.1fs", time.monotonic() - started)


def main() -> int:
    started = time.monotonic()

    import echo_bot

    run_stage("echo_bot", echo_bot.set_stage)
    for name in STAGES:
        run_stage(name, echo_bot.set_stage)

    echo_bot.set_stage("run")
    logging.getLogger("run").info("total %.1fs", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())