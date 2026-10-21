---
name: article-qrt
description: "Write a promo quote-tweet for an article — the client's own (self-QRT) or a teammate's — so people want to click and read it."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, formats, qrt]
    related_skills: [content-pipeline, client-voice]
---

# Article promo QRT

Run the `content-pipeline` (light: lint, fact-check, one critic pass). From the SOP (Format D):
"The QRT is promotion. Make people WANT to click and read the article."

- 3–6 sentences in **this** client's voice (not the author's).
- Lead with the single most useful or surprising thing in the article, with a real number from it.
- **Teammate QRT:** name the relationship naturally ("my co-founder", "our head of ads") and stack
  one credential; say why *this* reader should read it. **Self-QRT:** add what didn't fit in the
  article, or the one line you'd want them to steal.
- Never just "great read" / "must read". No hashtags, no em dashes.

Save with `kind: "qrt_promo"`, `parent_client` = the article's client, `parent_code` = its code,
and `target_url` if the article is already live. Until it's live the card says "post the article
first"; it gets an Open in X link automatically once the article is posted.
