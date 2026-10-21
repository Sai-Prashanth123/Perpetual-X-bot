# Perpetual X

An AI agency assistant for X (Twitter), built on [Hermes Agent](https://github.com/NousResearch/hermes-agent)
with Claude. **Everything happens in Slack.**

- **Perpetual (DM @Perpetual X):** the main agent. Ask it anything, as you would any AI assistant,
  plus agency questions ("how are we doing?", "what's viral in cold email?"). It also adds,
  changes and removes clients when you ask.
- **One channel per client (`#x-<handle>`):** that client's own agent works there. It hunts
  rising tweets and drafts tweets, threads, replies, quotes and reposts in the client's voice.
  Every draft arrives as a **card** with **Approve / Edit / Reject / Post now / Open in X**
  buttons. It learns from every click, edit and real result.
- **`#main-x-agent`:** reports only (a daily digest, a weekly report and weekly learning).
  Nobody chats there.
- **The Home tab** (click Perpetual X → Home) is the dashboard. It shows a leaderboard of every
  account with followers, views and their changes, a 28-day trend, engagement, suggestion use and
  attention flags. It also shows the viral feed with **Draft for…**, team activity, what's being
  learned, and a **Details** pop-up per client.

## Add a client: just ask
> **You → Perpetual X:** add client @alexvacca, run by Shravya, niche cold email and Clay GTM, watch @michlieben

Perpetual then:
1. checks the handle on X and adds it to the scraping console,
2. creates the public channel `#x-alexvacca` and invites Shravya and you,
3. sets up the client's agent and restarts for about 20 seconds.

Within a few minutes the client's agent learns the voice from their posts and says hello in the
channel. Already made a channel? Invite the bot and say "#my-channel is for @alexvacca, run by Shravya".

## Posting to X
| Draft | **Post now** (one tap) | **Open in X** |
|---|---|---|
| Tweet, thread, reply to someone who engaged with the client | ✓ once X is connected | ✓ |
| Reply, quote or repost on someone else's tweet | — (X doesn't allow apps to do this) | ✓ |

To connect a client's X account, ask Perpetual for a **Connect X** link and open it while logged
in to X as the client. The link goes to a small connector page, the only web page in the system.
Only a person can post; the AI has no posting tool.

## How it runs (free)
```
Oracle Cloud (free)   Hermes multiplexed gateway: Perpetual (default profile) + one profile per client
Neon (free)           Postgres: clients, suggestions, feedback, outcomes, metrics, viral feed, X tokens
Render (free)         web/: the "Connect X" OAuth page only
```

| Path | What it is |
|---|---|
| `lib/perpetual_core/` | Shared code: console client, ledger, agency views, Slack UI (`slackui.py`), X posting (`xpost.py`), schema, jobs |
| `hermes-template/plugins/perpetual/` | Client agent tools (X data, ledger) and `/post` |
| `hermes-template/plugins/perpetual-hq/` | Perpetual's tools, client management (`clients.py`), all Slack interactivity (`slackapp.py`) |
| `hermes-template/skills/`, `hermes-template/hq/` | Client-agent and Perpetual personas and skills |
| `docker/reconcile.py` | Builds profiles, routes, schedules and channel allowlists from the clients table (at boot, and again when clients change by chat) |
| `deploy/` | Server setup and `docker-compose.bot.yml` |
| `web/` | The X connector |
| `slack-app-manifest.json` | The Slack app (Socket Mode, Home tab, buttons) |

## Slack app setup (once)
Create the app from `slack-app-manifest.json`. If you created it from an older manifest, add these
in the app settings and reinstall:
- **Bot scopes:** `channels:manage` and `channels:join`.
- **App Home:** turn on the Home tab.

## Deploy
- **Bot:** on the server, run `git pull && docker compose -f deploy/docker-compose.bot.yml --env-file deploy/.env up -d --build`
- **Connector:** Render redeploys from `render.yaml`.
- **Tests:** `tests/test_slackui.py` (offline) and `tests/smoke_plugin.py` (needs `DATABASE_URL`).
