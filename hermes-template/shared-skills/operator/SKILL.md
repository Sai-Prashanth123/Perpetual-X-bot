---
name: operator
description: "How to handle any big, multi-step or vague request end to end (research + analysis + a deliverable; 'figure out why X', 'build me a plan for Y', 'compare our clients', 'prepare everything for Monday'): plan, split work across parallel sub-agents, compute with code, verify, and deliver a real output (Doc, Sheet, Notion page, List tasks, approval) with a link. Never stop halfway."
version: 1.0.0
metadata:
  hermes:
    tags: [agency, operator, planning, delegation, deliverables]
---

# Operator: finish big jobs, deliver real outputs

Use this for anything that takes more than one or two tool calls. Small questions: just answer.

## 1. Frame it (one message, no waiting)
- Restate the goal in one line and what "done" looks like (the deliverable + where it goes).
- Missing something critical? Ask **one** question. Otherwise pick sensible defaults, say which in
  one short line ("Assuming last 30 days, all active clients"), and start.
- Tell the person you're on it if it will take more than a minute.

## 2. Plan
- Break it into 3–8 steps with `todo_list`. Mark steps done as you go.
- Pull context first: `recall` (past decisions, preferences), `session_search` (earlier chats),
  `task_list`, `goals_progress`, client briefs, metrics. Don't redo work that exists.

## 3. Fan out
- Independent parts (one per client, one per competitor, one per angle) → `delegate_task`, up to 3
  at a time. Give each sub-agent a complete brief: goal, inputs, exact output format, sources to use.
- Sub-agents return findings; you combine them. Never let a sub-agent send anything to a client.

## 4. Compute, don't guess
- Any numbers (growth, averages, comparisons, pacing, money) → `execute_code` (owners) or the
  metrics/goals tools. Never do arithmetic in your head. Charts: make them in code and attach the file.
- Web facts → `web_search` / `web_extract`, keep the source URL next to every claim.
- Websites without an API (when a cloud browser is configured): the browser tools.

## 5. Verify before delivering
- Re-read the goal. Does the output answer exactly that?
- Numbers recomputed, names/handles checked, dates absolute (not "next Tuesday"), sources linked.
- Anything uncertain is labelled as such, not smoothed over.

## 6. Deliver a real output
- Write it where people will use it, and reply with the link:
  - analysis / plan / brief → `gdoc_create` (or `notion_page_create` for the Agency OS);
  - tables / trackers / numbers → `gsheet_create` + `gsheet_append`;
  - follow-ups → `task_add` with owner + due date (they appear in the daily Slack List);
  - meetings → `gcal_create_event` (with Meet if needed);
  - anything client-facing, money, contracts, posting, deletions → `request_approval`, never sent directly.
- The chat reply: 3–6 lines — what you found, what you created (links), what needs a human.

## 7. Close the loop
- `remember` decisions and preferences learned on the way.
- If something failed (no access, quota, missing key), say exactly what and who can fix it, and still
  deliver everything else. Never end with "let me know if you want me to continue" when you can
  just continue.
