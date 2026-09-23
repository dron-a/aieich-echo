#!/usr/bin/env python3
"""
Scheduler entrypoint. Runs both bots in one dyno, in order.

  echo_bot  reminders -- time-critical, runs first and always
  mail_bot  new mail  -- runs second, on whatever echo_bot left behind

One process rather than two scheduler jobs: Heroku Scheduler cannot order
two jobs, and a second dyno would pay another boot and another Neon cold
start for work that is not time-critical.

mail_bot is imported inside the function, after echo_bot has finished, so
a missing mail config or a broken import cannot stop reminders going out.
"""

import logging
import sys
import time
import os

cal_module = os.environ.get("CALENDAR_MODULE","cal_bot_monthly")
mail_module = os.environ.get("MAIL_MODULE", "mail_bot")
group_module = os.environ.get("GROUP_MODULE", "group_bot")
course_module = os.environ.get("COURSE_MODULE", "course_bot")
content_module = os.environ.get("CONTENT_MODULE", "content_bot")

def main() -> int:
    started = time.monotonic()
    log = logging.getLogger("run")

    import echo_bot

    echo_bot.main()

    # Each stage is isolated: reminders have already gone out, so nothing
    # downstream is worth failing the run for. Imports are lazy so a missing
    # config or a broken module cannot stop the stages before it.
    # Order is dependency-driven, not arbitrary. course_bot fills
    # dim_taxila_course; content_bot and group_bot both read course ids from
    # it, so a newly registered user is fully set up in one pass instead of
    # three. mail_bot is first among these because notifications are the
    # only part users notice promptly.
    for name in (mail_module, course_module, cal_module, content_module, group_module):
        try:
            __import__(name).main()
        except Exception:
            logging.getLogger(name).exception("%s failed", name)

    log.info("total %.1fs", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())