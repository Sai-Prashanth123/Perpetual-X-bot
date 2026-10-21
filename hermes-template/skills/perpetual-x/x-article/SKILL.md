---
name: x-article
description: "Write an X Article (long-form) for the client: 900-1,100 words, structured, fact-checked, with an 11-word title. Then ask teammates for promo QRTs."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, formats, article]
    related_skills: [content-pipeline, article-qrt, client-voice]
---

# X Article

Run the whole `content-pipeline`. Format rules (from the agency's article SOP,
`agency-writing-standards/references/x-article-sop.md`, and the client's `references/x-articles.md`
if present):

- **Title:** about 11 words: a specific outcome + a power word + the mechanism. Audit it with
  `agency-writing-standards/references/article-title-audit.md`. No clickbait, no colon soup.
- **Length:** 900–1,100 words.
- **Shape:** hook (80–120 words) → context (150–200) → framework (200–300) → case study from the
  client's receipts (200–300) → playbook the reader can copy (150–250) → close (50–100).
- H2/H3 headers about every 250 words, in sentence case. Paragraphs of 4 sentences or fewer. 1–2
  lists. Bold 1–3 times. **Zero em dashes.**
- Pick a different angle from anything in `content_history`, and from teammates' recent articles.

Write it as plain text: the **title on the first line**, then the body (use `## ` for headers).
Save with `kind: "article"`. Then call `request_article_qrts` with its code so same-team teammates
(and this client, as a self-QRT) write promo QRTs.
