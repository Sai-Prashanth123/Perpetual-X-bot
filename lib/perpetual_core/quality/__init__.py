"""Quality gates for every draft: AI-pattern linting, fact-checking with references, and the
blind-lineup test. The two linters are vendored unchanged from the agency's content-agent repo
(tools/lint_post.py, scripts/ai_pattern_lint.py); both are stdlib-only.
"""

import json
import random
import re
import subprocess
import sys
from pathlib import Path

from . import lint_post

HERE = Path(__file__).parent


# ---------------------------------------------------------------- linting

def lint(text, lint_config=None, platform="x"):
    """Run both linters. Returns {"pass", "hard": [...], "review": [...]} with one line per finding."""
    config = dict(lint_post.DEFAULT_CONFIG) if hasattr(lint_post, "DEFAULT_CONFIG") else {}
    if lint_config:
        config.update(lint_config)
    config.setdefault("platform", platform)
    findings, _stats = lint_post.lint(text, config)
    hard = [f"{f.rule}: {f.match}" + (f" → {f.fix}" if f.fix else "") for f in findings if f.severity == "HARD"]
    review = [f"{f.rule}: {f.match}" for f in findings if f.severity == "REVIEW"]
    try:
        r = subprocess.run([sys.executable, str(HERE / "ai_pattern_lint.py"), "--json"], input=text,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        ai = json.loads(r.stdout or "{}")
        hard += [f"ai-pattern: {h['rule']}" + (f" ({h['detail']})" if h.get("detail") else "") for h in ai.get("hard", [])]
        review += [f"ai-pattern: {h['rule']}" + (f" ({h['detail']})" if h.get("detail") else "") for h in ai.get("review", [])]
    except (subprocess.SubprocessError, ValueError) as e:
        review.append(f"ai-pattern lint unavailable: {e}")
    return {"pass": not hard, "hard": hard, "review": review[:25]}


# ---------------------------------------------------------------- fact-checking

NUM = re.compile(r"(?<![\w.])(\$?\d[\d,.]*\s?(?:%|[kKmMbB]\b|x\b|X\b|\+)?)(?:\s?(?:million|billion|thousand))?")
NAMED = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\b")
SKIP_NAMES = {"I", "The", "A", "An", "And", "But", "If", "We", "You", "It", "This", "That", "My", "Our", "Here",
              "What", "When", "Why", "How", "Step", "Most", "Every", "One", "No", "So", "Then", "They", "He", "She"}


def norm_number(token):
    """'$7M' / '7 m' / '7,000,000' / '7 million' -> comparable forms."""
    t = token.lower().replace(",", "").replace(" ", "").replace("$", "")
    forms = {t}
    mult = {"k": 1e3, "m": 1e6, "b": 1e9, "thousand": 1e3, "million": 1e6, "billion": 1e9}
    for suffix, factor in mult.items():
        if t.endswith(suffix):
            try:
                forms.add(str(int(float(t[: -len(suffix)]) * factor)))
            except ValueError:
                pass
    try:
        val = float(t.rstrip("%x+"))
        forms.add(str(int(val)) if val.is_integer() else str(val))
    except ValueError:
        pass
    return {f for f in forms if f}


def parse_do_not_say(receipts_md):
    """Rows of the ⛔ DO-NOT-SAY table in receipts.md: [(pattern, why)]."""
    rows, in_table = [], False
    for line in (receipts_md or "").splitlines():
        if re.search(r"do.not.say", line, re.I):
            in_table = True
            continue
        if in_table:
            if line.startswith("#"):
                break
            cells = [c.strip(" `*") for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and not set(cells[0]) <= set("-: ") and cells[0].lower() not in ("pattern", "phrase", "claim"):
                rows.append((cells[0], cells[1]))
    return rows


def _source_lines(receipts_md):
    """Receipt lines that can back a claim; the DO-NOT-SAY table is excluded (it lists banned claims)."""
    out, in_dns = [], False
    for line in (receipts_md or "").splitlines():
        if line.startswith("#"):
            in_dns = bool(re.search(r"do.not.say", line, re.I))
            continue
        if line.strip() and not in_dns:
            out.append(line.strip())
    return out


def fact_check(text, receipts_md="", claim_bank=None, real_posts=None):
    """Every number and named entity in `text`, traced to a source.

    Returns {"pass", "claims": [{"claim", "status", "source"}], "references": [...]}, where status is
    verified | do_not_say | unsourced. A draft passes only with no do_not_say and no unsourced numbers.
    """
    claims, refs = [], []
    lines = _source_lines(receipts_md)
    bank = (claim_bank or {}).get("claims", []) if isinstance(claim_bank, dict) else (claim_bank or [])
    posts = real_posts or []
    low_text = text.lower()

    for pattern, why in parse_do_not_say(receipts_md):
        hit = False
        try:
            hit = bool(re.search(pattern, text, re.I))
        except re.error:
            hit = pattern.lower() in low_text
        if hit:
            claims.append({"claim": pattern, "status": "do_not_say", "source": f"receipts.md DO-NOT-SAY: {why}"})

    def find_source(forms, raw):
        for line in lines:
            line_forms = set()
            for tok in NUM.findall(line):
                line_forms |= norm_number(tok)
            if forms & line_forms:
                return f"receipts.md: \"{line[:140]}\""
        for c in bank:
            nums = set()
            for n in c.get("numbers", []):
                nums |= norm_number(str(n))
            if forms & nums:
                prov = c.get("provenance") or {}
                where = prov.get("url") or prov.get("doc_id") or ""
                return f"claim:{c.get('claim_id')} {where} — \"{(prov.get('quote') or c.get('text') or '')[:120]}\""
        for p in posts:
            for tok in NUM.findall(p.get("text") or ""):
                if forms & norm_number(tok):
                    return f"client post {p.get('url')}"
        return None

    seen = set()
    for raw in NUM.findall(text):
        raw = raw.strip()
        digits = re.sub(r"\D", "", raw)
        if not digits or (len(digits) == 4 and raw.isdigit() and 1990 <= int(raw) <= 2035) or raw in seen:
            continue                      # years are context, not claims
        if raw.isdigit() and int(raw) <= 10 and not re.search(r"[%$kKmMbBx+]", raw):
            continue                      # list counters / "3 steps" are structure, not claims
        seen.add(raw)
        src = find_source(norm_number(raw), raw)
        claims.append({"claim": raw, "status": "verified" if src else "unsourced", "source": src})
        if src and src not in refs:
            refs.append(src)

    corpus = (receipts_md or "").lower() + " ".join((c.get("text") or "").lower() for c in bank) \
        + " ".join((p.get("text") or "").lower() for p in posts)
    for m in NAMED.finditer(text):
        name = m.group(0)
        before = text[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:>-•" or text[:m.start()].endswith("\n")
        if name.split()[0] in SKIP_NAMES or len(name) < 3 or (sentence_start and " " not in name):
            continue                      # a capitalised first word is grammar, not a named entity
        if name.lower() in corpus or any(c["claim"] == name for c in claims):
            continue
        claims.append({"claim": name, "status": "check", "source": None})

    bad = [c for c in claims if c["status"] in ("do_not_say", "unsourced")]
    return {"pass": not bad, "claims": claims, "references": refs,
            "summary": f"{sum(c['status'] == 'verified' for c in claims)} verified, "
                       f"{sum(c['status'] == 'unsourced' for c in claims)} unsourced, "
                       f"{sum(c['status'] == 'do_not_say' for c in claims)} do-not-say, "
                       f"{sum(c['status'] == 'check' for c in claims)} names to eyeball"}


# ---------------------------------------------------------------- blind lineup (Judge 0)

def lineup_prompt(draft, real_posts, seed=None):
    """Shuffle the draft among real posts; the judge must say which one (if any) is AI-written."""
    rng = random.Random(seed)
    items = [p for p in real_posts if p.strip()][:5] + [draft]
    rng.shuffle(items)
    answer = items.index(draft) + 1
    body = "\n\n".join(f"### Post {i}\n{t.strip()}" for i, t in enumerate(items, 1))
    instructions = ("Below are posts that one person published on X. Exactly one of them MAY have been written "
                    "by an AI imitating them. Identify the post most likely to be AI-written and how confident "
                    "you are (LOW, MEDIUM, HIGH), with the concrete tells. If nothing stands out, say LOW.")
    return instructions, body, answer


LINEUP_SCHEMA = {"type": "object", "properties": {
    "suspect": {"type": "integer"}, "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
    "tells": {"type": "array", "items": {"type": "string"}}}, "required": ["suspect", "confidence", "tells"]}


def lineup_verdict(parsed, answer):
    caught = parsed.get("suspect") == answer and parsed.get("confidence") in ("MEDIUM", "HIGH")
    return {"pass": not caught, "caught": caught, "confidence": parsed.get("confidence"),
            "judge_pick": parsed.get("suspect"), "draft_position": answer,
            "tells": parsed.get("tells", [])[:6] if caught else []}
