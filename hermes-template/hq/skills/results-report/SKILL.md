---
name: results-report
description: "Client results vs goals: weekly internal summary (who is off pace and what to change) and a monthly client-facing report draft per client, published for the team and sent to the client only through an approval."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency, goals, reporting]
---

# Results vs goals

Goals come from each client's strategy doc and live in `client_goals` (`goals_set`, `goals_list`,
`goals_remove`). Progress is measured from the daily metrics up to yesterday: actual, expected by now
(straight-line pace), pct, status `ahead` / `on_track` / `behind` / `no_data`.

Setting goals in chat: "Alex: +2,000 followers by Dec 31" → `goals_set` client alex, metric
followers_gain, target 2000, period total, end_date 2026-12-31. "500k impressions a month" →
impressions 500000 per_month. "40 replies per post" → replies 40 per_post. "2 posts a day" → posts 2
per_day. Confirm the goal back in one line, with today's progress.

## Weekly (internal, reports channel)

1. `goals_progress` (no client). No goals at all → reply exactly `[SILENT]`.
2. Reply (posted to the reports channel), ≤ 20 lines:
   - **Off pace first:** per client behind, one line per goal: `@client · goal: actual vs expected (pct)`,
     plus `needed_per_day` for total goals, then one concrete lever (more posts, reply sessions, a
     thread, a stronger hook) grounded in `client_detail` / `agency_overview`, not generic advice.
   - Then one line listing clients on track / ahead.
   - `no_data` goals: say "no data yet" and why (`reason`); if metrics are missing for days, say so.

## Monthly (client-facing draft, 1st of the month)

For each client with goals (one at a time):
1. `goals_progress` client, `client_detail` client (last 30 days: posts, best posts, growth).
2. Write the report for the client: friendly and plain. Sections: **Goals and where we are** (each
   goal: target, result, on track or not, in words a client understands), **What worked** (2–3 best
   posts with numbers), **What we change next month** (2–3 specific actions). No internal notes, no
   team names, no blame.
3. Publish it for the team as ONE document with `results_publish` (or `publish_document` where that
   tool exists), title "Results: <Month YYYY> · @client", summary one line.
4. Then `request_approval` with kind `slack_message` (client Slack Connect channel) or `email`
   (client email), the draft as the message body and the document link. **Never send to a client
   directly**: only the approval sends it, after a person approves.
5. Reply with one line per client: published link + approval requested.

Never invent numbers, posts or goals. If a client has no goals, skip it and list it at the end as
"no goals set: ask the account lead to add them from the strategy doc".
