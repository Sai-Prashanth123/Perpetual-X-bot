---
name: content-studio
description: "The writing team every piece goes through (posts, threads, articles, long tweets, QRTs, replies): researcher, hook hunter and strategist in parallel, three writers, critics, then an editor who ships the winner plus two alternates. Use for any writing task; FAST mode for replies and trend-jacks."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, writing, studio]
    related_skills: [content-pipeline, client-voice, agency-writing-standards, doc-templates, x-article, x-long-tweet, article-qrt, video-qrt, reply-pack]
---

# Content studio (a writing team, not one draft)

You are the editor. You run a small team of subagents with `delegate_task`, then you decide.
The quality bar is `content-pipeline`; this skill is how the team gets there.

## Before you start

- **Subagents know nothing.** Every goal you hand out must carry: the client handle, a voice
  summary (5-8 rules from `client-voice`: case, length, rhythm, banned words, two example lines),
  a receipts excerpt (the real numbers and stories they may use, from `references/receipts.md`),
  the brief (topic, kind, goal), and the exact tools to use. Keep each goal short.
- **Cost.** One `delegate_task` batch per stage. Never loop a subagent. On this small server
  `delegate_task` concurrency is limited, so a batch may run partly one after another. That is
  fine; do not split it up yourself.
- **Mode.** Use FULL for posts, threads, articles, long tweets and QRTs. Use FAST for replies and
  trend-jacks (see the end).

## Stage 1. Research (one parallel batch of 3)

1. **RESEARCHER.** Tools: `web_search` (max 3 calls), `web_extract`, `x_find_tweets`. Goal: fresh
   facts, data and examples on the brief from the last 14 days. Return each with its source link.
   No opinions, no drafting.
2. **HOOK HUNTER.** Tools: `hook_research` on the topic, `hooks_library` (action `best`), and the
   client's own best hooks (`x_client_content`, top posts by views). Return the 5 strongest hook
   patterns, each with a proof link and its metrics (views, likes, bookmarks).
3. **STRATEGIST.** Tools: `content_history`, `research_memory`, `postmortems` (action `recent`),
   and the `playbook` skill. Return: the angle to take, what to avoid (repeats, flopped hooks,
   do-not-say items), and how the piece fits the client's goal.

## Stage 2. Write (one parallel batch of 3)

4. Give 3 WRITERS the same pack: voice summary, receipts excerpt, the researcher's facts with
   links, the strategist's angle and avoid list, and the format guide for this kind
   (`x-article`, `x-long-tweet`, `article-qrt` and so on, summarised).
5. Each writer gets a **different hook pattern** from the hook hunter and a different angle.
6. Rule for all of them: only facts from the researcher or the receipts. No invented numbers,
   names or quotes. Return the draft and the hook pattern used.

## Stage 3. Critics

7. Run `lint_draft` and `fact_check` on all 3 drafts yourself (tools, in one go).
8. Run `blind_lineup` on the best 2 (by lint and fact-check results).
9. Send one CRITIC subagent all 3 drafts plus the voice summary. It scores each 1-10 on voice,
   hook, substance and one idea, and names the weak lines word for word.

## Stage 4. Edit and ship (you)

10. Pick the winner. Graft the best lines from the runners-up into it.
11. Fix every critic hit: HARD lint hits, unsourced or do-not-say claims, lines the lineup judge
    caught, lines the critic named.
12. Re-run `lint_draft` and `fact_check` until both are clean (max 3 passes; if it still fails,
    say what blocked it in one line).
13. Score it with the x-algo scorer (`agency-writing-standards/references/x-algo-scorer.md`).
14. Save the winner with `suggest_save` (`cards: false`): `references` from fact_check,
    `factcheck`, `scores` `{voice, hook, substance, anti_ai, lineup, algo}`, `hook` (the pattern
    label), format, topic, rationale.
15. Save the 2 runner-ups too, lint-clean, with `batch` `studio-alt`.
16. Record the chosen hook with `hooks_library` (action `use`).

## Output

- **Called from another skill** (weekly-package, x-article, reply-pack and so on): return the
  saved codes (winner first, then the alternates) and let that skill publish.
- **Asked directly:** one `publish_document` (kind `answer`, or `package` for several pieces)
  with: the final piece, why this hook (with the hook hunter's proof links), the 2 alternates,
  the scores, and the sources. Pass `asked_by_slack` and `dm_to` when you have them.
- Asked for several pieces: run the stages once per piece, and publish one document at the end.

## FAST mode (replies and trend-jacks)

1. One batch: HOOK HUNTER only (keep it to `hooks_library` and the target post's context).
2. One batch: 2 WRITERS, different hooks, same pack as above.
3. `lint_draft` and `fact_check` on both. Fix, pick, save (`cards: false`, the target fields
   filled). No critic subagent, no lineup, no alternates batch.
