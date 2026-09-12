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


def main() -> int:
    started = time.monotonic()
    log = logging.getLogger("run")

    import echo_bot

    echo_bot.main()

    # Each stage is isolated: reminders have already gone out, so nothing
    # downstream is worth failing the run for. Imports are lazy so a missing
    # config or a broken module cannot stop the stages before it.
    for name in ("mail_bot", "cal_bot"):
        try:
            __import__(name).main()
        except Exception:
            logging.getLogger(name).exception("%s failed", name)

    log.info("total %.1fs", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())