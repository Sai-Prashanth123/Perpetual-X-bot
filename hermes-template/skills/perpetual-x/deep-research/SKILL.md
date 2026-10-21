---
name: deep-research
description: "Deep research on THIS client: own performance, competitors and peers, the niche on the web, growth levers. Saves findings to research_memory; on request, publishes a research document. Use for 'what's working', 'how do we grow', 'review our profile', and the scheduled research run."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, research]
    related_skills: [doc-templates, playbook, client-voice]
---

# Deep research (one client, evidence first)

Budget per run: at most **6 `web_search` calls** and **4 `web_extract` calls**, because web searches
are metered. X data from the console tools is not metered.

## 0. Start from memory
Read `research_memory`. Don't redo findings that are fresh (under 3 days old) unless asked; focus
on gaps and on what changed.

## 1. Run four threads, in parallel where you can
Use `delegate_task` with up to 4 subagents. Give each one its full context: the handle, the niche
phrases and watch accounts from `client-brief`, the exact question, and which tools to use. Each
returns findings with numbers and sources.

- **A. Own performance (30 days).** Use `x_client_analytics` (summary, rhythm) and
  `x_client_content` (days 30).
  - Find which formats, hooks, topics, lengths and posting hours win and lose, by median
    views/likes/bookmarks, not single outliers.
  - Name the 3 best and 3 worst posts, and why.
- **B. Competitors and peers.** Use `x_user_tweets` for the watch accounts and 2-3 peers you find.
  - What gets them reach that this client doesn't do?
  - List their best posts this fortnight, with links.
- **C. The niche on the web.** Use `web_search` on the niche phrases (news, launches, reports and
  data from the last 7-14 days), and `x_find_tweets` for what's hot on X.
  - For each opportunity: what happened, the link, and the angle this client can own with its
    receipts.
- **D. Growth levers.** Look at:
  - `x_user_info` on the client (bio, followers);
  - their reply strategy (who they reply to, how often);
  - their cadence vs peers;
  - collaboration options with teammates.

  For each lever, give a concrete change plus the evidence for it.

## 2. Synthesise like a strategist
Say what is working, what is not, and why. Then give the 3-5 moves that would grow the account
fastest, ranked by expected impact. Be specific and honest: numbers, links, examples. No generic
advice.

## 3. Remember
Run `research_memory` with action=add, one entry per thread (kind: performance / competitors /
niche / growth), each with its sources. Note any hypothesis worth testing for `playbook`.

## 4. Deliver
- **Asked by a person (or Perpetual):** run `publish_document` (kind `research`) with the research
  template from `doc-templates`. Pass `asked_by_slack` and `dm_to` when you have them.
- **Scheduled run:** save to memory only, then reply exactly `[SILENT]`.
