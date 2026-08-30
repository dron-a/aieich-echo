# Deploying echo_bot to Heroku

## Before the first push

Heroku's Python buildpack supports uv natively. It needs `pyproject.toml`,
`uv.lock` and `.python-version` in the repo root, and no other package
manager files.

- [ ] `uv.lock` is committed (not gitignored)
- [ ] `.python-version` exists
- [ ] `requirements.txt`, `runtime.txt`, `Pipfile`, `poetry.lock` are all
      gone — any of them conflicts with `uv.lock`
- [ ] Only the `heroku/python` buildpack is attached. A third-party uv
      buildpack will generate a `requirements.txt` and break the build.

Verify against Heroku's Python support reference if the build fails; their
docs win over this file.

## Deploy

```bash
heroku create <app-name>
heroku config:set \
  DATABASE_URL="..." \
  EVOLUTION_API_URL="https://<whatsmiau-app>/v1" \
  EVOLUTION_API_KEY="..." \
  EVOLUTION_INSTANCE="..." \
  GROQ_API_KEY="..." \
  GROQ_MODEL="openai/gpt-oss-20b" \
  GEMINI_API_KEY="..." \
  OPENROUTER_API_KEY="..." \
  LEAD_MINUTES=0 \
  LLM_DEADLINE_S=90

# Multi-line value, set on its own
heroku config:set SUBJECT_CONTEXT="$(cat subjects.txt)"

git push heroku main
```

Nothing runs on deploy. The `worker` process type exists so the app builds;
leave it scaled at 0. Scheduler uses one-off dynos, not this worker.

## Schema

Run `schema.sql` against the Neon database before the first scheduled run.

## Scheduler

Do this last, once the schema exists and a manual run has succeeded.

```bash
heroku addons:create scheduler:standard
heroku addons:open scheduler
```

Job command: `python echo_bot.py`

Frequency: **hourly**. Do not use every-10-minutes unless `LEAD_MINUTES=45`
is genuinely needed — runs that close together keep the Neon compute from
ever suspending, which burns the free tier's 100 CU-hours in days.

## Smoke test

```bash
heroku run python echo_bot.py
```

Needs all config vars and the schema in place; the bot reads required vars
at import and exits with a `KeyError` if any are missing.

A healthy run logs, in order: wake ping, connect time, pending count,
whatsmiau healthy, purge count, dispatch slot, candidates vs due, sent
count, and `run finished in Xs (Ys connected)`.

## Notes

- Neon free tier: 100 CU-hours and 0.5 GB per project, compute suspends
  after 5 minutes idle. Hourly runs cost roughly 65 of those 100 hours,
  mostly idle tail. Keep test runs on a separate project.
- If CU-hours run out, the compute suspends until the next billing period
  and every run fails until then.
- Failed sends are not retried. The notice fires again at its next
  scheduled hour.
- A staged update that fails validation stays `processed = FALSE` and is
  retried every hour indefinitely. Watch for a row that never clears.
