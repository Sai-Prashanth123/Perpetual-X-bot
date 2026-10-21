#!/usr/bin/env python3
"""
ai_pattern_lint.py — Deterministic AI-prose linter for ColdIQ X content.

The FIRST gate in the AI-Scrub Layer (mirrors linkedin-postmaker/lint_post.py).
Runs before the adversarial critic panel. Catches the mechanical tells a model
rationalizes in its own draft. HARD violations block output. REVIEW flags are
handed to the critics as evidence to refute against.

Usage:
    python3 ai_pattern_lint.py path/to/draft.md
    echo "TEXT" | python3 ai_pattern_lint.py
    python3 ai_pattern_lint.py path/to/draft.md --json

Exit codes: 0 = no HARD violations (REVIEW flags may still print), 1 = HARD.
"""
import re, sys, json, statistics
from pathlib import Path

# Windows stdout defaults to cp1252 and client posts carry emoji, so any
# script that echoes real content dies mid-print without this. errors=
# "replace" keeps a rare unmappable glyph from taking the run down.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent

# ---------- load ----------
def load_text():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        return open(args[0], encoding="utf-8").read()
    return sys.stdin.read()


def client_slug():
    """Optional --client <slug> (or 2nd positional) enables per-client voice
    overrides: a banned-vocab word this client genuinely uses in their real
    corpus is THEIR voice, not an AI tell, so it downgrades HARD -> REVIEW.
    Mirrors Mai-Lan's scrubber Phase-1 override logic, but derived from data."""
    argv = sys.argv[1:]
    if "--client" in argv:
        i = argv.index("--client")
        if i + 1 < len(argv):
            return argv[i + 1]
    pos = [a for a in argv if not a.startswith("--")]
    return pos[1] if len(pos) > 1 else None


def corpus_vocab_allow(slug, vocab):
    """Words from `vocab` the client uses >=2x organically in their real corpus."""
    if not slug:
        return set()
    p = ROOT / "data/clients" / slug / "cache/exemplar-candidates.json"
    if not p.exists():
        return set()
    try:
        blob = " ".join(c.get("text", "") for c in json.load(open(p, encoding="utf-8")).get("candidates", [])).lower()
    except Exception:
        return set()
    allow = set()
    for w in vocab:
        if " " in w or "-" in w:
            continue  # never auto-allow multiword AI phrases
        if len(re.findall(r"\b" + re.escape(w) + r"(?:s|es|ed|ing|d)?\b", blob)) >= 2:
            allow.add(w)
    return allow

def body_only(text):
    # drop a leading title line (first non-empty line) for prose-rhythm stats
    lines = text.splitlines()
    out, dropped = [], False
    for ln in lines:
        if not dropped and ln.strip():
            dropped = True
            continue
        out.append(ln)
    return "\n".join(out)

def sentences(text):
    # strip markdown bullets/headers so list items don't fuse into 1 "sentence"
    cleaned = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^#{1,6}\s+", "", s)          # headers
        s = re.sub(r"^[-*•>]\s+", "", s)           # bullets/quotes
        cleaned.append(s)
    joined = " ".join(cleaned)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", joined) if p.strip()]
    return parts

def wcount(text):
    return len(re.findall(r"\b[\w'$%]+\b", text))

HARD, REVIEW = [], []
def hard(label, detail=""):   HARD.append((label, detail))
def review(label, detail=""): REVIEW.append((label, detail))

text = load_text()
body = body_only(text)
sents = sentences(body)
lens = [len(s.split()) for s in sents]
wc = wcount(text)
per1k = (lambda n: round(n / max(wc, 1) * 1000, 1))

# ============================================================ HARD =========

# 1) em / en dashes and their fake substitutes
if text.count("—"): hard("EM DASH", f'{text.count(chr(0x2014))} found — banned, zero tolerance')
if text.count("–"): hard("EN DASH", f'{text.count(chr(0x2013))} found — banned')
if " -- " in text:       hard("DOUBLE-HYPHEN as em-dash", '" -- " reads as AI; rewrite the sentence')
for ln in text.splitlines():
    s = ln.strip()
    if re.match(r"^[-*•>]\s", s):   # skip markdown bullets / quotes
        continue
    for m in re.finditer(r"[a-z]+ - [a-z]+", s):   # literal spaced hyphen mid-sentence
        review("Spaced hyphen as dash", m.group(0))

# 2) AI-vocabulary blacklist (never acceptable in ColdIQ voice)
#    Merged with Mai-Lan's ai-trait-scrubber library (2B) so every client's
#    engine gets the full list, not just Frontal's. See references/ai-trait-scrubber.md.
ai_vocab = [
    "leverage", "unlock", "crucial", "essential", "landscape", "realm",
    "testament", "navigate", "delve", "dive into", "dive deep", "unpack",
    "synergy", "synergies", "game-changer", "game changer", "supercharge",
    "seamless", "seamlessly", "robust", "myriad", "tapestry", "beacon",
    "ever-evolving", "ever-changing", "in essence", "at the end of the day",
    "let that sink in", "read that again", "needle-moving", "move the needle",
    "thrilled to", "elevate", "harness", "paradigm", "ecosystem of",
    # standing feedback bans (memory: banned reaction-filler phrases,
    # no-built-around-bridge) — zero tolerance in any client voice
    "built around", "stuck with me", "hit hardest", "hits hardest",
    "hit me hardest", "what struck me",
    # --- from Mai-Lan's scrubber 2B (verbs) ---
    "utilize", "embrace", "cultivate", "foster", "streamline", "underscore",
    "embark", "empower", "elucidate", "facilitate", "revolutionize", "reimagine",
    # --- adjectives ---
    "groundbreaking", "revolutionary", "transformative", "cutting-edge",
    "scalable", "pivotal", "comprehensive", "meticulous", "holistic",
    "next-level", "pioneering", "disruptive", "unprecedented",
    # --- nouns / metaphors ---
    "cornerstone", "catalyst", "key takeaways", "actionable insights",
    "best practices", "deep dive",
    # --- phrases ---
    "in today's landscape", "in today's fast-paced world", "as we navigate",
    "make no mistake", "circle back", "touch base", "think outside the box",
    "in a world where", "let's dive in", "let's explore", "let's unpack",
    "at its core", "in conclusion", "the truth is", "something shifted",
    "everything changed",
]
vocab_hits, vocab_allowed = [], []
low = text.lower()
_slug = client_slug()
_allow = corpus_vocab_allow(_slug, ai_vocab)
# single-word entries tolerate common verb/adjective inflections (leverage ->
# leverages/leveraged/leveraging); multiword phrases match literally.
for w in ai_vocab:
    if " " in w or "-" in w:
        pat = r"\b" + re.escape(w) + r"\b"
    else:
        pat = r"\b" + re.escape(w) + r"(?:s|es|ed|ing|d)?\b"
    n = len(re.findall(pat, low))
    if not n:
        continue
    if w in _allow:
        vocab_allowed.append(f"{w} x{n}")   # client's own word -> REVIEW, not HARD
    else:
        vocab_hits.append(f"{w} x{n}")
if vocab_hits: hard("AI VOCAB", "; ".join(vocab_hits))
if vocab_allowed:
    review(f"AI-vocab words {_slug} uses organically (verify each is voice, not slop)",
           "; ".join(vocab_allowed))

# 3) filler transitions (>2 across the whole piece = HARD)
fillers = [
    "here's the thing", "here's what i mean", "to make this concrete",
    "let me tell you", "let's dive", "the reality is", "make no mistake",
    "here's the kicker", "the key is", "what's more", "in today's",
    "it's important to note", "that being said", "needless to say",
    "when it comes to", "in conclusion", "to sum up",
]
fhits = [(f, low.count(f)) for f in fillers if low.count(f)]
if sum(n for _, n in fhits) > 2:
    hard("FILLER TRANSITIONS", "; ".join(f"{f} x{n}" for f, n in fhits))
elif fhits:
    review("Filler transition", "; ".join(f"{f} x{n}" for f, n in fhits))

# 4) not-X-but-Y family (>3 = HARD, the parallel-scaffolding tell)
nxby = [
    r"\bnot\s+\w+,?\s+but\s+\w+",
    r"isn'?t\s+\w+[.,]\s+it'?s\s+\w+",
    r"does(?:n'?t|\s+not)\s+\w+\.\s+(?:it|they|we)\s+\w+",
    r"do(?:n'?t|\s+not)\s+\w+\.\s+(?:it|they|we)\s+\w+",
    r"\bit'?s\s+not\s+\w+[.,]\s+it'?s\s+\w+",
    r"isn'?t\s+(?:about|just)\s+\w+",
]
nxby_found = []
for p in nxby:
    nxby_found += re.findall(p, text, re.IGNORECASE)
if len(nxby_found) > 3:
    hard("NOT-X-BUT-Y (parallel scaffolding)", f"{len(nxby_found)}: " + " | ".join(nxby_found[:6]))
elif nxby_found:
    review("Not-X-but-Y pivot", f"{len(nxby_found)}: " + " | ".join(nxby_found[:6]))

# 4b) CONTRASTIVE-CONSTRUCTION BUDGET (the big gap).
#     AI overuses antithesis. The quality-bar allows ~2 GENUINE contrasts per
#     long piece. This counts EVERY contrastive form the old regex missed and
#     enforces the budget. >4 = HARD, >=2 = REVIEW.
contrastive = []
def _c(label, frag): contrastive.append((label, frag.strip()))
# "X, not Y" / "X, never Y" / "X rather than Y" / "X instead of Y" (comma-negation tail)
for m in re.finditer(r"[\w%)]+,\s+(?:not|never|rather than|instead of)\s+\w+", text, re.I):
    _c("comma-negation 'X, not Y'", m.group(0))
# "... not the <noun>. The <noun> is ..."  (the "not the point. The point is" pivot)
for m in re.finditer(r"\bnot\s+the\s+\w+\.\s+(?:the\s+\w+|it)\s+is\b", text, re.I):
    _c("'not the X. The X/It is' pivot", m.group(0))
# assert-then-pivot: "is/was never|not <...>. <it/that/the/someone/nobody> ..." and "...never <...>. <Cap> just/only"
for m in re.finditer(r"\b(?:is|was|are|were)\s+(?:almost\s+never|never|not)\b[^.]{0,45}\.\s+(?:it|that|the|someone|nobody|that's)\b", text, re.I):
    _c("assert-then-pivot", m.group(0))
for m in re.finditer(r"\b(?:is|was|are|were)\s+never\b[^.]{0,45}\.\s+[A-Z]\w+\s+(?:just|only|simply)\b", text):
    _c("assert-then-pivot (never...just)", m.group(0))
# within-sentence handed/kept (or automated X / kept Y) antithesis — the inversion the Hunter caught
for m in re.finditer(r"\b(?:handed|gave|let|put|automated|outsourced)\b[^.]{0,70}\band\s+kept\b", text, re.I):
    _c("within-sentence handed/kept antithesis", m.group(0))
# "is/are not the same(thing)" negation-equation
for m in re.finditer(r"\b(?:is|are|was|were)\s+not\s+the\s+same\b", text, re.I):
    _c("'not the same' equation", m.group(0))
# contrastive negation tails grafted onto a claim
for ph in ["none of it came from", "nothing to do with it", "has nothing to do",
           "had nothing to do", "not because", "not the point"]:
    if ph in low: _c("negation tail", ph)
# antithesis / anaphora PAIRS at ANY length: adjacent sentences sharing an
# antithesis-prone first word (catches "Manage the.../Manage them...",
# "When that.../When it...", "Build it.../Wire it...")
_anaph = {"manage","when","spend","give","grade","build","wire","put","read","most","every","you"}
for a, b in zip(sents, sents[1:]):
    fa, fb = a.lower().split(), b.lower().split()
    if fa[:1] and fb[:1] and fa[0] == fb[0] and fa[0] in _anaph:
        _c("antithesis/anaphora pair", f"«{a[:38]}…» «{b[:38]}…»")
    elif fa[:1] and fb[:1] and ("and you get" in a.lower() and "and you get" in b.lower()):
        _c("antithesis/anaphora pair", f"«{a[:38]}…» «{b[:38]}…»")
# de-dup on fragment text
_seen = set(); contrastive = [c for c in contrastive if not (c[1].lower() in _seen or _seen.add(c[1].lower()))]
n_contrast = len(contrastive)
if n_contrast > 4:
    hard("CONTRASTIVE OVERLOAD (antithesis / not-X-but-Y scaffolding)",
         f"{n_contrast} constructions (budget ~2): " + " | ".join(f"[{t}] {x}" for t, x in contrastive[:10]))
elif n_contrast >= 2:
    review("Contrastive constructions over budget",
           f"{n_contrast} (budget ~2): " + " | ".join(f"[{t}] {x}" for t, x in contrastive[:10]))

# ========================================================== REVIEW ========
# Stylistic smells. Not auto-blocking, but every one is HANDED TO THE CRITICS
# as evidence. The AI-Tell Hunter must rule on each.

# 5) sentence-rhythm
if len(lens) >= 6:
    sd = statistics.stdev(lens)
    if sd < 7:
        review("Sentence-length stdev too low (monotone)", f"stdev={sd:.1f} (<7)")
    # machine-gun runs: 3+ consecutive <=5 words
    runs, run, examples = 0, 0, []
    buf = []
    for s, l in zip(sents, lens):
        if l <= 5:
            run += 1; buf.append(s)
        else:
            if run >= 3: runs += 1; examples.append(" / ".join(buf))
            run, buf = 0, []
    if run >= 3: runs += 1; examples.append(" / ".join(buf))
    if runs > 2:
        review("Machine-gun fragment runs", f"{runs} runs; e.g. {examples[0] if examples else ''}")
    # a SINGLE run of 4+ consecutive ultra-short (<=4w) sentences = staccato list tell
    frun, fbuf, fex = 0, [], []
    for s, l in zip(sents, lens):
        if l <= 4:
            frun += 1; fbuf.append(s)
        else:
            if frun >= 4: fex.append(" / ".join(fbuf))
            frun, fbuf = 0, []
    if frun >= 4: fex.append(" / ".join(fbuf))
    if fex:
        review("Ultra-short fragment stack (staccato list)", fex[0])

# 6) antithesis / echo closer — adjacent short sentences that mirror structure
#    catches "Same tools as everyone else. Different hands at the two ends."
for a, b in zip(sents, sents[1:]):
    aw, bw = a.split(), b.split()
    if 2 <= len(aw) <= 8 and 2 <= len(bw) <= 8:
        pair = f"«{a}» «{b}»"
        if re.match(r"^(Same|Different|Not|Always|Never)\b", a) and re.match(r"^(Same|Different|Not|Always|Never|But)\b", b):
            review("Antithesis/echo closer (ghostwriter polish)", pair)
        elif a.rstrip(".!?").lower().split()[0:1] == b.rstrip(".!?").lower().split()[0:1] and len(aw) <= 6:
            review("Anaphora (twin sentence openers)", pair)

# 7) assert-then-undercut — sentence then a <=4-word negating fragment
#    catches "It looks finished. It frequently isn't."
neg = re.compile(r"\b(isn'?t|aren'?t|wasn'?t|won'?t|doesn'?t|don'?t|can'?t|frequently isn'?t|often isn'?t|usually isn'?t)\b", re.I)
for a, b in zip(sents, sents[1:]):
    if len(b.split()) <= 4 and neg.search(b):
        review("Assert-then-undercut micro-beat", f"«{a}» «{b}»")

# 8) aphorism closers: standalone short "The X is the Y." equation sentences
aph = [s for s in sents if re.match(r"^(The\s+\w+\s+is\s+the\s+\w+|[\w']+\s+is\s+the\s+\w+)\.?$", s) and len(s.split()) <= 6]
if len(aph) >= 2:
    review("Aphorism/equation closers (essayist tell)", " | ".join(f"«{x}»" for x in aph[:5]))

# 9) polish-adverb intensifiers (editorializing the reader doesn't need)
adv = ["lovingly","quietly","faithfully","precisely","genuinely","simply","truly",
       "frankly","honestly","literally","actually","merely","utterly","deeply",
       "endlessly","blindly","silently","desperately"]
adv_hits = [(w, len(re.findall(r"\b"+w+r"\b", low))) for w in adv]
adv_hits = [(w,n) for w,n in adv_hits if n]
adv_total = sum(n for _,n in adv_hits)
if per1k(adv_total) > 4:
    review("Polish-adverb density (intensifier slop)", f"{adv_total} ({per1k(adv_total)}/1k): " + ", ".join(f"{w}x{n}" for w,n in adv_hits))

# 10) essayist tropes
tropes = ["the whole game","the entire game","the only thing that","the only part of",
          "that's exactly why","which is exactly","what makes it","the moat now",
          "stops being the edge","is the moat","here's what changed","the strategic point"]
tr_hits = [(t, low.count(t)) for t in tropes if low.count(t)]
if tr_hits:
    review("Essayist trope phrases", "; ".join(f"{t} x{n}" for t,n in tr_hits))

# 11) rhetorical scaffolding openers (Picture/Imagine/Stack/Start with/Here's)
scaff = re.compile(r"^(Picture |Imagine |Think about |Consider |Stack |Start with |Here'?s |Now picture)", re.I)
sc = [s for s in sents if scaff.match(s)]
if len(sc) >= 2:
    review("Rhetorical scaffolding openers", " | ".join(f"«{x[:50]}…»" for x in sc[:4]))

# 12) And/But/So sentence-opener density
conj = sum(1 for s in sents if re.match(r"^(And|But|So)\b", s))
if conj >= 4:
    review("Conjunction-opener density", f"{conj} sentences open with And/But/So")

# 13) tricolon: "a, b, and c" noun triplets (flag if dense)
tricolon = re.findall(r"\b(\w+),\s+(\w+),\s+and\s+(\w+)\b", text)
if len(tricolon) >= 3:
    review("Tricolon density (triadic list rhythm)", f"{len(tricolon)} triplets")

# ===== semantic AI-tells ported from Mai-Lan's ai-trait-scrubber (2C) =====
# These are the tells that make every client sound the SAME. Regex-detectable
# ones live here (deterministic floor); the judgment ones (narrator distance,
# earned profundity, density) are the scrubber stage's job.

# 14) copulative avoidance (scrubber 3C-24): inflated linking verbs dodging "is/has"
copula = re.findall(r"\b(serves as|stands as|marks a|represents a|boasts a|"
                    r"features a|plays a (?:key |vital |crucial )?role in|"
                    r"aims to|seeks to|helps to)\b", low)
if copula:
    review("Copulative avoidance (inflated linking verbs — use 'is'/'has'/direct verb)",
           "; ".join(f"{c}" for c in dict.fromkeys(copula)))

# 15) participle trap (scrubber 3C-25): vague -ing tails that fake depth
participle = re.findall(r"\b(highlighting|underscoring|paving the way|reflecting the|"
                        r"marking a new|signaling a shift|showcasing|emphasizing|"
                        r"reinforcing|paving)\b[^.,\n]{0,40}", low)
if participle:
    review("Participle trap (-ing tail with no named consequence)",
           " | ".join(p.strip() for p in participle[:5]))

# 16) puffery / significance inflation (scrubber 3C-26)
puffery = [ph for ph in ["a pivotal moment","a major shift","broader implications",
           "a defining era","a watershed moment","a turning point","a game changer",
           "a paradigm shift","the future of","a new era"] if ph in low]
if puffery:
    review("Puffery / significance inflation (weight-word with no before/after)",
           "; ".join(puffery))

# 17) false ranges (scrubber 3C-27): impressive spans that mean nothing
franges = re.findall(r"\bfrom\s+\w+(?:\s+\w+)?\s+to\s+\w+(?:\s+\w+)?\b", text)
franges = [f for f in franges if not re.search(r"\bfrom\s+\$?\d", f)]  # keep real numeric ranges
_falsey = [f for f in franges if re.search(r"(startups?|enterprises?|boardrooms?|"
           r"break rooms?|coast|ancient|modern|garage|fortune)", f, re.I)]
if _falsey:
    review("False range (span that covers 'all of it')", " | ".join(_falsey[:4]))

# 18) correlative crutches (scrubber 3C-14)
corr = []
if re.search(r"\bnot only\b[^.\n]{0,60}\bbut also\b", low): corr.append("not only...but also")
if re.search(r"\bwhether\b[^.\n]{0,40}\bor\b", low): corr.append("whether...or")
if re.search(r"\bon (?:the )?one hand\b", low): corr.append("on one hand...on the other")
if corr:
    review("Correlative crutch (rewrite as direct statement)", "; ".join(corr))

# 19) elegant variation openers + false agency (scrubber 3C-20): abstract noun + human verb
false_agency = re.findall(r"\b(the data|the numbers|the strategy|the decision|the process|"
                          r"the framework|the system|complaints|the market|the trend)\s+"
                          r"(tells?|shows?|reveals?|decides?|realizes?|evolves?|emerges?|"
                          r"knows?|wants?|believes?)\b", low)
if false_agency:
    review("False agency (abstract noun doing a human action — name the person)",
           "; ".join(f"{a} {b}" for a,b in false_agency[:5]))

# 20) knowledge-cutoff disclaimers (scrubber 3C-30) — HARD, never legit in a post
kc = [ph for ph in ["as of my last update","based on available information",
      "i do not have real-time access","to the best of my knowledge as of",
      "as an ai","i cannot browse"] if ph in low]
if kc:
    hard("KNOWLEDGE-CUTOFF DISCLAIMER", "; ".join(kc))

# 21) title-case headers (scrubber 3C-29): corporate-deck templating tell
for ln in text.splitlines():
    s = re.sub(r"^[#>*\-\s]+", "", ln).strip()
    words = s.split()
    if 3 <= len(words) <= 8 and not s.endswith(('.', '!', '?', ':')):
        caps = sum(1 for w in words if w[:1].isupper())
        small = {"a","an","the","for","and","of","to","in","on","with","vs"}
        contentw = [w for w in words if w.lower() not in small]
        if contentw and all(w[:1].isupper() for w in contentw) and caps >= 3:
            review("Title-case header (use sentence case)", s[:60]); break

# ---------- report ----------
if "--json" in sys.argv:
    print(json.dumps({"word_count": wc,
                      "hard": [{"rule": l, "detail": d} for l, d in HARD],
                      "review": [{"rule": l, "detail": d} for l, d in REVIEW]}, indent=2))
else:
    print(f"AI-PATTERN LINT  |  words: {wc}  |  sentences: {len(sents)}  |  stdev: {statistics.stdev(lens):.1f}" if len(lens) >= 6 else f"AI-PATTERN LINT | words: {wc}")
    print("=" * 64)
    if HARD:
        print(f"\n  ❌ HARD VIOLATIONS ({len(HARD)}) — must fix before output:")
        for l, d in HARD: print(f"     • {l}: {d}")
    else:
        print("\n  ✅ No HARD violations.")
    if REVIEW:
        print(f"\n  ⚠️  REVIEW FLAGS ({len(REVIEW)}) — hand to the critic panel as evidence:")
        for l, d in REVIEW: print(f"     • {l}: {d}")
    else:
        print("\n  ✅ No REVIEW flags.")
    print()

sys.exit(1 if HARD else 0)
