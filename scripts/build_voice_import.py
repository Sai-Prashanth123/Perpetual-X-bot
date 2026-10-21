"""Build a Perpetual X import from a content-agent checkout: clients + their full voices.

    python scripts/build_voice_import.py <content-agent-dir> <out-dir> [--x-only]

Output (never committed — client voice packs are private; copy it to the bot server):
  <out>/clients.json                       one entry per client: handle, name, niche, watch, avoid
  <out>/<handle>/client-voice/SKILL.md     identity + voice + quality bar + voiceprint + banned terms
  <out>/<handle>/client-voice/references/  receipts, content plan, exemplars, X-format voices, extras
  <out>/<handle>/playbook/SKILL.md         seeded from the content plan (pillars as hypotheses)
  <out>/_shared/agency-writing-standards/  master SOPs, AI-trait scrubber, critics, X algorithm & hooks

The reconciler seeds these into each client's profile only while the client's voice is still
unlearned, so nothing the agent has learned since is ever overwritten.
"""

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
X_ONLY = "--x-only" in sys.argv
TODAY = date.today().isoformat()

# Extra, client-specific voice material beyond data/clients/<slug>/law/ (paths relative to the repo).
EXTRAS = {
    "alex-vacca": {"x-tweets.md": "skills/coldiq-x-tweetpost-writer/references/alex-voice-tweets.md",
                   "x-threads.md": "skills/coldiq-x-thread-writer/references/alex-voice-threads.md",
                   "x-articles.md": "skills/coldiq-x-article-writer/references/alex-voice.md"},
    "michel-lieben": {"x-tweets.md": "skills/coldiq-x-tweetpost-writer/references/mich-voice-tweets.md",
                      "x-threads.md": "skills/coldiq-x-thread-writer/references/mich-voice-threads.md",
                      "x-articles.md": "skills/coldiq-x-article-writer/references/mich-voice.md",
                      "voice-checker.md": "skills/mich-voice-checker/SKILL.md"},
    "nick-velkovski": {"writer-voice.md": "skills/nick-velkovski-x-writer/memory/voice.reference.md",
                       "critics.md": "skills/nick-velkovski-x-writer/memory/critics.md",
                       "heyreach-playbook.md": "skills/heyreach-jake-patterns/references/playbook.md"},
    "vuk-vukosavljevic": {"x-examples-and-corrections.md": "skills/vuk-vukosavljevic-x-writer/references/x-examples-and-corrections.md",
                          "heyreach-playbook.md": "skills/heyreach-jake-patterns/references/playbook.md"},
    "vick-mucunski": {"heyreach-playbook.md": "skills/heyreach-jake-patterns/references/playbook.md"},
    "tycho-luijten": {"voice-and-comedy-engine.md": "skills/tycho-luijten-linkedin-writer/references/voice-and-comedy-engine.md",
                      "claims-bank.md": "skills/tycho-luijten-linkedin-writer/references/claims-bank.md",
                      "content-pillars.md": "skills/tycho-luijten-linkedin-writer/references/content-pillars.md"},
    "mai-lan-khong": {"ai-trait-scrubber.md": "skills/mai-lan-khong-writer/ai-trait-scrubber.md"},
}
EXTRA_DIRS = {  # whole folders of curated voice notes
    "mai-lan-khong": "data/clients/mai-lan-khong/knowledge-base/second-brain",
    "pierre-herubel": "data/clients/pierre-herubel/knowledge-base/second-brain",
    "jonathan-cronstedt": "data/clients/jonathan-cronstedt/knowledge-base",
}
SHARED = {
    "x-tweet-sop.md": "skills/coldiq-x-tweetpost-writer/references/master-sop.md",
    "x-thread-sop.md": "skills/coldiq-x-thread-writer/references/master-sop.md",
    "x-article-sop.md": "skills/coldiq-x-article-writer/references/master-sop.md",
    "ai-trait-scrubber.md": "skills/content-writer/references/ai-trait-scrubber.md",
    "critic-panel.md": "skills/content-writer/references/critic-panel.md",
    "ai-scrub-critics.md": "skills/coldiq-x-article-writer/references/ai-scrub-critics.md",
    "x-algorithm-signals.md": "skills/x-algo-scorer/references/x-algorithm-signals.md",
    "hook-velocity-patterns.md": "skills/x-algo-scorer/references/hook-velocity-patterns.md",
    "proven-formats-2026.md": "skills/video-qrt-writer/references/proven-formats-2026.md",
    "meme-native-formats.md": "skills/video-qrt-writer/references/meme-native-formats.md",
}
# Things that must never be copied: credentials sometimes live inside skill text.
SECRET = re.compile(r"(\b\d{8,10}:[A-Za-z0-9_-]{30,}\b|ntn_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,})")


def read(rel):
    p = SRC / rel
    return SECRET.sub("[redacted]", p.read_text(encoding="utf-8")) if p.exists() else None


def strip_frontmatter(text):
    if text and text.startswith("---"):
        parts = text.split("---", 2)
        return parts[2].lstrip() if len(parts) == 3 else text
    return text or ""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def frontmatter(name, description, version, tags):
    return (f"---\nname: {name}\ndescription: \"{description}\"\nversion: {version}\n"
            f"metadata:\n  hermes:\n    tags: [{', '.join(tags)}]\n---\n\n")


def niche_from(vocab, company):
    """Search phrases: the client's vocab minus the brand names, most specific first."""
    skip = {company.lower(), "b2b", "agency", "framework", "playbook", "system"} if company else set()
    phrases = [v for v in vocab if v.lower() not in skip and len(v) > 2]
    phrases.sort(key=lambda v: (-len(v.split()), vocab.index(v)))
    return phrases[:8]


def build_client(slug, intel):
    cfg = json.loads((SRC / "config" / f"{slug}.json").read_text(encoding="utf-8"))
    handle = (cfg.get("x_handle") or "").lstrip("@")
    if X_ONLY and not handle:
        return None
    name, company = cfg.get("display_name") or slug, cfg.get("company") or ""
    law = SRC / "data" / "clients" / slug / "law"
    out = OUT / handle.lower() / "client-voice"
    refs = out / "references"

    core = []
    for title, fname in (("Identity", "identity.md"), ("Voice", "voice.md"), ("Quality bar", "quality-bar.md"),
                         ("Voiceprint (measured from their real posts)", "voiceprint.md")):
        text = read(f"data/clients/{slug}/law/{fname}")
        if text:
            core.append(f"## {title}\n\n{strip_frontmatter(text).strip()}\n")
    banned = list(cfg.get("banned_terms") or [])
    lint = law / "lint-config.json"
    if lint.exists():
        try:
            lc = json.loads(lint.read_text(encoding="utf-8"))
            for key in ("banned_phrases", "banned_terms", "banned_words", "forbidden"):
                banned += [b for b in lc.get(key, []) if isinstance(b, str)]
        except ValueError:
            pass
    ci = (intel.get("clients") or {}).get(slug) or {}
    meta = {"handle": handle.lower(), "name": name, "company": company,
            "niche_topics": niche_from(ci.get("vocab") or [], company),
            "watch_accounts": [f"@{s['handle']}" for s in (cfg.get("sentinels") or []) if s.get("handle")][:12],
            "avoid_accounts": [f"@{a}" for a in (ci.get("banned_authors") or [])]}
    if not core:
        # No voice pack: leave the voice unlearned so the client's agent learns it from real posts.
        return {**meta, "references": 0, "voice": "learn from posts"}
    if banned:
        core.append("## Never use\n\n" + "\n".join(f"- {b}" for b in dict.fromkeys(banned)) + "\n")

    ref_list = []
    for fname in ("receipts.md", "content-plan.md", "exemplars.md", "engagement-playbook.md"):
        text = read(f"data/clients/{slug}/law/{fname}")
        if text:
            write(refs / fname, text)
            ref_list.append(fname)
    for fname, rel in EXTRAS.get(slug, {}).items():
        text = read(rel)
        if text:
            write(refs / fname, strip_frontmatter(text))
            ref_list.append(fname)
    if slug in EXTRA_DIRS and (SRC / EXTRA_DIRS[slug]).is_dir():
        for p in sorted((SRC / EXTRA_DIRS[slug]).glob("*.md")):
            write(refs / f"kb-{p.name}", read(str(p.relative_to(SRC))))
            ref_list.append(f"kb-{p.name}")

    guide = "\n".join(f"- `references/{r}`" for r in ref_list)
    body = (f"# Client voice — {name} (@{handle}), {company}\n\n"
            f"Imported from the agency's content-agent voice pack on {TODAY}. This is the curated, "
            "human-approved voice: write exactly to it. The weekly learning review refines it from "
            "real results and the team's edits, but never removes these rules without strong evidence.\n\n"
            + "\n".join(core)
            + f"\n## Go deeper (read when the task needs it)\n\nProof and claims you may use, the content "
              f"plan, best posts and format-specific guides:\n{guide}\n\n"
              "Only use facts and numbers that appear in these files or the client's real posts. "
              "Anything else goes in as a [placeholder].\n")
    write(out / "SKILL.md", frontmatter("client-voice",
          f"{name}'s writing voice on X: identity, voice rules, quality bar, voiceprint, banned terms, and references to receipts, content plan and best posts. Load before writing ANY copy for the client.",
          "1.0.0", ["perpetual-x", "voice", "imported"]) + body)

    plan = read(f"data/clients/{slug}/law/content-plan.md")
    pb = (f"# Playbook — {name}\n\nSeeded on {TODAY} from the content plan in the voice pack. Treat the pillars "
          "and formats below as **hypotheses** until real results confirm them; the weekly learning "
          "review rewrites this with evidence.\n\n" + (strip_frontmatter(plan) if plan else "No content plan in the pack yet.\n"))
    write(OUT / handle.lower() / "playbook" / "SKILL.md", frontmatter("playbook",
          f"What works and what doesn't for {name} on X, with evidence. Seeded from the content plan; rewritten weekly by learning-review.",
          "1.0.0", ["perpetual-x", "playbook", "learned"]) + pb)

    return {**meta, "references": len(ref_list), "voice": "imported"}


def build_shared():
    out = OUT / "_shared" / "agency-writing-standards"
    names = []
    for fname, rel in SHARED.items():
        text = read(rel)
        if text:
            write(out / "references" / fname, strip_frontmatter(text))
            names.append(fname)
    body = ("# Agency writing standards\n\nThe agency's house rules for X, used across every client. The "
            "client's own voice always wins on tone and wording; these govern craft and quality.\n\n"
            "Before showing any draft:\n"
            "1. **Format SOP** — follow the SOP for the format: tweets `references/x-tweet-sop.md`, threads "
            "`references/x-thread-sop.md`, long-form/articles `references/x-article-sop.md`.\n"
            "2. **Hook** — check the first line against `references/hook-velocity-patterns.md` and "
            "`references/x-algorithm-signals.md` (what the algorithm rewards: replies, bookmarks, dwell).\n"
            "3. **De-AI pass** — scrub with `references/ai-trait-scrubber.md`; no em-dash chains, no "
            "\"here's the thing\", no rule-of-three filler, no hype words.\n"
            "4. **Critics** — run the draft past `references/critic-panel.md`; fix anything a critic flags.\n"
            "5. **Formats that are working now** — `references/proven-formats-2026.md`, `references/meme-native-formats.md`.\n\n"
            "References:\n" + "\n".join(f"- `references/{n}`" for n in names) + "\n")
    write(out / "SKILL.md", frontmatter("agency-writing-standards",
          "The agency's house rules for writing on X: format SOPs (tweet, thread, article), hook and algorithm signals, AI-trait scrubber, critic panel, proven formats. Load before writing drafts.",
          "1.0.0", ["perpetual-x", "agency", "standards"]) + body)
    return names


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if OUT.exists():
        shutil.rmtree(OUT)
    registry = json.loads((SRC / "config" / "clients.json").read_text(encoding="utf-8"))
    intel = json.loads((SRC / "config" / "intelligence" / "per-client.json").read_text(encoding="utf-8"))
    clients = [c for c in (build_client(s, intel) for s in registry.get("active", [])) if c]
    write(OUT / "clients.json", json.dumps({"source": "content-agent", "built": TODAY, "clients": clients}, indent=2))
    shared = build_shared()
    for c in clients:
        print(f"@{c['handle']:<16} {c['name']:<28} niche={len(c['niche_topics'])} watch={len(c['watch_accounts'])} refs={c['references']}")
    print(f"{len(clients)} clients, shared standards: {len(shared)} references -> {OUT}")


if __name__ == "__main__":
    main()
