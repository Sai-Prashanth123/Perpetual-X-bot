---
name: content-pipeline
description: "The quality pipeline every draft goes through before it's saved: frame, check history, draft in voice, lint, de-AI scrub, fact-check with references, critics, blind lineup, algo score. Load for ANY writing task."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, quality]
    related_skills: [client-voice, playbook, agency-writing-standards, x-article, x-long-tweet, article-qrt, video-qrt]
---

# Content pipeline (human-grade, fact-checked, referenced)

Nothing reaches the team unless it clears this. If a draft can't clear it in 3 passes, don't ship
it: say what blocked it (in one line) instead.

1. **Load the law.** `client-voice` (+ its `references/receipts.md` and the format guide for this
   kind), `playbook`, `agency-playbook`, `agency-writing-standards`.
2. **Frame.** One idea, one receipt (a real number or story from receipts / their posts), one
   emotional spark, one enemy (the belief or habit it argues against).
3. **No repeats.** `content_history` — skip any idea, hook or receipt used in the last 45 days.
4. **Draft** in the client's voice, following the format skill's structure.
5. **Lint.** `lint_draft` → fix every HARD hit (em dashes, AI-tell words, "not X but Y", tricolons,
   banned terms). Re-run until it passes.
6. **De-AI scrub.** Read the draft against `agency-writing-standards/references/ai-trait-scrubber.md`:
   false agency, narrator-from-a-distance, elegant variation, symmetry, hedging, summary closers.
   If more than ~30% of lines need changes, rewrite from the voice examples instead of patching.
7. **Fact-check.** `fact_check` → remove or `[placeholder]` every **unsourced** number, never keep a
   **do_not_say** item, and sanity-check every **check** name. Every remaining claim needs a source.
8. **Critics.** Score the draft honestly 1–10 on Voice (would they say this?), Hook (first line
   stops the scroll), Substance (specific, true, useful), One idea. Anything under 8 → fix the
   cited lines and go back to 5. Max 3 passes.
9. **Blind lineup.** `blind_lineup` → if the judge catches it, rewrite the lines it named.
10. **Algo score.** Score with `agency-writing-standards/references/x-algo-scorer.md` (0–100).
11. **Save.** `suggest_save` with `references` (from fact_check), `factcheck` (its summary + claims),
    `scores` `{voice, hook, substance, anti_ai, lineup, algo}`, plus hook / format / topic / rationale,
    and `cards: false`.
12. **Publish.** Put the saved drafts (codes, copy, Open in X links, scores, fact-check, sources) in ONE
    document with `publish_document`, laid out per `doc-templates`. Only a single quick draft someone
    asked for may go out as a card instead (leave `cards` on).
