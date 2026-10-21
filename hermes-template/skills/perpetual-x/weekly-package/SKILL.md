---
name: weekly-package
description: "Produce this client's weekly content package: 1 X article (+ teammate/self promo QRTs requested), 1 long tweet, and any extras in the package (e.g. video QRTs). Then DM a summary."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, package]
    related_skills: [content-pipeline, x-article, x-long-tweet, video-qrt, article-qrt]
---

# Weekly package

1. Read the package from `client-brief` (default: 1 article, 1 long tweet, article QRTs on).
2. `content_history` and `x_trends` once — pick topics that are fresh for this client and timely.
3. **Article** → `x-article` (full pipeline) → save → `request_article_qrts` with its code.
4. **Long tweet** → `x-long-tweet` on a *different* idea from the article.
5. **Extras** → e.g. `video_qrt: 2` → two separate `video-qrt` drafts (different source posts).
6. `notify_team` with one line: what was delivered, by code
   ("Package: A4 article 'How we…', L7 long tweet, V2+V3 video QRTs; promo QRTs requested from @a, @b").
7. Your channel reply: a 2-line header. The cards carry the drafts.

If a piece can't clear the pipeline, deliver the rest and say which piece is blocked and why.
