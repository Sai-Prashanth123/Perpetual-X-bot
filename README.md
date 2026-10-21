# Perpetual X — agency edition

An always-on X (Twitter) strategist for every client of the agency, built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) with Claude.

- **Each employee runs one client** from that client's Slack channel. The bot hunts for tweets
  to reply to, quote and repost, writes tweet and thread ideas in the client's voice, replies to
  people who engaged, and reports what works.
- **Approved posts go out with one tap.** `/post T3` in Slack, or **Post now** on the dashboard.
- **The owner sees everything:** a web dashboard (leaderboard, growth, viral feed, competitors,
  team, learning) and a **Main X agent** channel with a daily digest and weekly report.
- **It learns:** every approval, edit, rejection and real result feeds each client's playbook
  every week. Patterns that hold across clients go into a shared agency playbook. Each client's
  voice stays its own.

## How it's built

```
Render
├─ agency-bot   ONE Hermes container, multiplexed gateway, ONE Slack app
│    ├─ default profile = Main X agent      → #main-x-agent
│    └─ one profile per client in clients.yaml → its own channel (gateway.profile_routes)
├─ agency-web   FastAPI dashboard · X account connections · /internal/post
└─ agency-db    Postgres: suggestions, feedback, outcomes, daily metrics, viral feed, tokens
```

| Path | What it is |
|---|---|
| `clients.yaml` | The client roster: handle, Slack channel, employee, niche, watch list, timezone. |
| `lib/perpetual_core/` | Shared code: settings, scraping-console client, ledger, agency views, schema, jobs. |
| `hermes-template/plugins/perpetual/` | Client tools (X data, ledger) and the human-only `/post` command. |
| `hermes-template/plugins/perpetual-hq/` | HQ tools: scorecards, team, viral feed, agency playbook. |
| `hermes-template/skills/perpetual-x/` | One skill per client job, plus the learned `client-voice` and `playbook`. |
| `hermes-template/hq/` | HQ persona and skills (digest, report, agency learning). |
| `docker/reconcile.py` | Runs on every boot. Builds profiles, routes, schedules and the Main X agent from `clients.yaml`. |
| `web/` | The dashboard. |
| `slack-app-manifest.json` | The one Slack app. Paste it at api.slack.com/apps. |
| `render.yaml` | Bot + web + Postgres. |

## Posting: what X allows

Since February 2026, X lets apps post only original tweets, threads, and replies to people who
mentioned or quoted the client. So:

| Suggestion | One tap (`/post`, Post now) | Open in X link |
|---|---|---|
| **T** tweet, **H** thread | ✓ | ✓ |
| **E** reply to someone who engaged with the client | ✓ | ✓ |
| **R** reply, **Q** quote, **P** repost on other people's tweets | — | ✓ (opens X with the copy ready) |

Only a person can post. The model has no posting tool.

## The schedule

| When | Client channel | Main X agent channel |
|---|---|---|
| First boot | Learns the voice from 90 days of posts and says hello | Backfills 4 weeks of numbers |
| Every 2 h, 08–20 | Reply, quote and repost hunt | — |
| :15 and :45, 07–22 | Breakout alerts (silent unless something is taking off) | — |
| 06:00 / 06:30 + 14:30 | — | Metrics and viral snapshots (no AI cost) |
| 08:00 | Tweet and thread ideas | — |
| 09:00 | — | Agency digest |
| 13:00 | Engage-back replies | — |
| Every 15 min | Dashboard requests ("draft for @client") | — |
| 21:00 | Daily recap | — |
| Mon | Weekly report (09:00) | Agency report (10:00) |
| Sun | Learning review (22:00) | Agency learning (23:30) |

Each client's times are in the client's timezone; the Main X agent uses `AGENCY_TIMEZONE`.

**In a client channel:**
- Feedback: `ok T3` · `edit R4: …` · `no Q2 too salesy` · `posted T3`
- Publish: `/post T3`
- Ask anything, e.g. paste a tweet URL, or "turn this into a tweet: …".

## Set up once

1. **Slack:** api.slack.com/apps → Create New App → From a manifest → paste
   `slack-app-manifest.json`. First replace the redirect URL with your dashboard's address.
   - Install the app.
   - Note the bot token (`xoxb-`), an app-level token with `connections:write` (`xapp-`), and the client ID and secret.
   - Create the Main X agent channel (e.g. `#main-x-agent`) and one channel per client, and invite the bot to each.
2. **X:** create a developer app at developer.x.com.
   - Type: Web App. Enable OAuth 2.0.
   - Callback URL: `<dashboard>/x/callback`.
   - Load pay-per-use credits.
   - Note the client ID and secret.
3. **Render:** push this repo to GitHub, then Render → New → Blueprint → pick the repo.
   - Fill in the values Render asks for (see `.env.example`).
   - Make `TOKEN_ENCRYPTION_KEY` with `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`.

## Add a client

```bash
scripts/new-client.sh theirhandle "Their Name" --channel C0123ABCDEF --employee U0123ABCDEF \
    --topics "cold email, Clay GTM" --watch "@peer1, @peer2" --tz Europe/London
git commit -am "add @theirhandle" && git push
```

Then open their page on the dashboard and connect their X account.

Use specific phrases for niche topics. They're searched exactly, so write "Clay GTM", not "clay".

## Run locally

```bash
docker run -d --name px-pg -e POSTGRES_PASSWORD=px -e POSTGRES_DB=perpetual -p 55432:5432 postgres:17-alpine
docker build -t perpetual-x-bot .
# bot: pass the env vars from .env.example plus DATABASE_URL pointing at px-pg
# dashboard:
PYTHONPATH=lib DATABASE_URL=postgresql://postgres:px@localhost:55432/perpetual DASHBOARD_DEV_LOGIN=1 \
  PUBLIC_URL=http://localhost:8000 uvicorn app:app --app-dir web
# tests:
DATABASE_URL=... XCONSOLE_API_KEY=... uv run --no-project --with "psycopg[binary]" tests/smoke_plugin.py
```

## Notes
- **What a redeploy never overwrites:** learned state (voices, playbooks, memories, the database).
  Job skills the agent has improved are also kept, unless `PX_RESEED_SKILLS=1`.
- **Changing the schedule:** edit `docker/reconcile.py` and bump `CRON_VERSION`.
- **Removing a client:** remove them from `clients.yaml`. This stops their jobs and keeps their history.
