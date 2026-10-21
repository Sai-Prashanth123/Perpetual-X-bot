#!/usr/bin/env python3
"""
lint_post.py — Shared configurable post linter for all client post-making workflows.

Usage:
  python3 ~/content-agent/tools/lint_post.py [--config path/to/lint-config.json] [--json] [file]
  echo "post text" | python3 ~/content-agent/tools/lint_post.py --config path/to/lint-config.json

Exit code 0 = no HARD violations (REVIEW flags may exist)
Exit code 1 = at least one HARD violation
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# This linter is the writer protocol's "incorruptible" gate: exit 1 = no ship.
# It prints ✅/⚠ status glyphs, and on Windows stdout is cp1252 — so
# format_human() raised UnicodeEncodeError *while printing the PASS message*
# and the process exited 1. The gate has therefore been returning 1 on every
# draft on this platform, pass or fail, which makes its signal worthless in
# exactly the direction that looks safe. Encoding first, before any output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str   # "HARD" or "REVIEW"
    rule: str
    line: int
    match: str
    fix: str


@dataclass
class Stats:
    para_count: int = 0
    multi_sentence_count: int = 0
    short_line_count: int = 0


# ---------------------------------------------------------------------------
# Base rules — always enforced
# ---------------------------------------------------------------------------

# HARD: em dash and en dash
DASH_PATTERN = re.compile(r"[—–]")

# HARD: AI-tell vocabulary (case-insensitive, word-boundary aware)
AI_TELL_HARD = [
    "leverage",
    "unpack",
    "deep dive",
    "dive deep",
    "synergy",
    "synergies",
    "ever-evolving",
    "at the end of the day",
    "let that sink in",
    "thrilled to announce",
    "it could be argued",
    "in essence",
    "landscape",
    "unlock",
    "crucial",
    "game-changer",
    "game changer",
    "mindset shift",
    "thought leader",
    "delve",
    "tapestry",
    "testament to",
    "navigate the",
    "in today's fast-paced",
]

# Build a single pattern per vocab term (longest phrases first to avoid partial-word issues)
AI_TELL_HARD_SORTED = sorted(AI_TELL_HARD, key=len, reverse=True)

def _vocab_pattern(term: str) -> re.Pattern:
    """Return a case-insensitive regex that matches the term as a whole phrase."""
    # Multi-word phrases: allow flexible whitespace
    escaped = r"\s+".join(re.escape(w) for w in term.split())
    # Wrap in word boundaries where applicable
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)

AI_TELL_HARD_PATTERNS = [(term, _vocab_pattern(term)) for term in AI_TELL_HARD_SORTED]

# HARD: "not X, it's Y" / "not X but Y" scaffolding
NOT_X_ITS_Y_PATTERN = re.compile(
    r"\b(?:it'?s|that'?s)\s+not\b.{1,60}?\bit'?s\b",
    re.IGNORECASE | re.DOTALL,
)

# REVIEW: soft vocabulary
SOFT_VOCAB_REVIEW = [
    "essential",
    "in order to",
    "furthermore",
    "moreover",
]
SOFT_VOCAB_PATTERNS = [(term, _vocab_pattern(term)) for term in SOFT_VOCAB_REVIEW]


# ---------------------------------------------------------------------------
# Platform rules
# ---------------------------------------------------------------------------

# LinkedIn: external links in body (http/https URLs)
EXTERNAL_LINK_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def _is_short_line(line: str) -> bool:
    """A 'staircase' short mono-line: non-empty, <= 80 chars, no sentence-ending punctuation mid-line."""
    stripped = line.strip()
    if not stripped:
        return False
    # Count as short if under 80 chars and feels like a single fragment
    return len(stripped) <= 80


def _sentence_count(para: str) -> int:
    """Rough sentence count in a paragraph block."""
    # Split on . ! ? followed by whitespace or end of string
    parts = re.split(r"[.!?]+(?:\s|$)", para.strip())
    return sum(1 for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "client": "unknown",
    "platform": None,
    "do_not_say_hard": [],
    "product_names_review": [],
    "banned_vocab_extra": [],
    "lane_gates_review": [],
    "skip_base_rules": [],
}


def load_config(path: Optional[str]) -> dict:
    if not path:
        return dict(DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    config = dict(DEFAULT_CONFIG)
    config.update(data)
    return config


# ---------------------------------------------------------------------------
# Core linting engine
# ---------------------------------------------------------------------------

def lint(text: str, config: dict) -> tuple[list[Finding], Stats]:
    findings: list[Finding] = []
    skip = set(config.get("skip_base_rules", []) or [])
    platform = (config.get("platform") or "").lower()

    lines = text.splitlines()

    # Pre-compute paragraphs (blocks separated by blank lines)
    paragraphs: list[str] = []
    current: list[str] = []
    for ln in lines:
        if ln.strip():
            current.append(ln)
        else:
            if current:
                paragraphs.append("\n".join(current))
                current = []
    if current:
        paragraphs.append("\n".join(current))

    # Stats
    stats = Stats()
    stats.para_count = len(paragraphs)
    multi_sentence = sum(1 for p in paragraphs if _sentence_count(p) >= 2)
    stats.multi_sentence_count = multi_sentence
    short_lines = [ln for ln in lines if _is_short_line(ln)]
    stats.short_line_count = len(short_lines)

    # ------------------------------------------------------------------
    # BASE RULES
    # ------------------------------------------------------------------

    # Em/en dash — HARD
    if "em_dash" not in skip:
        for i, line in enumerate(lines, 1):
            for m in DASH_PATTERN.finditer(line):
                char = m.group(0)
                label = "em dash" if char == "—" else "en dash"
                findings.append(Finding(
                    severity="HARD",
                    rule="dash",
                    line=i,
                    match=char,
                    fix=f"Remove {label} (use hyphen or restructure sentence)",
                ))

    # AI-tell vocabulary — HARD
    # banned_vocab_extra accepts plain strings or {"pattern":..., "label":...} objects
    if "ai_tell_vocab" not in skip:
        raw_extra = config.get("banned_vocab_extra") or []
        extra_patterns: list[tuple[str, re.Pattern]] = []
        extra_labels: dict[str, str] = {}
        for entry in raw_extra:
            if isinstance(entry, dict):
                pat_s = entry.get("pattern", "")
                lbl = entry.get("label", pat_s)
                if not pat_s:
                    continue
                try:
                    compiled = re.compile(pat_s, re.IGNORECASE)
                    extra_patterns.append((pat_s, compiled))
                    extra_labels[pat_s] = lbl
                except re.error:
                    pass
            else:
                extra_patterns.append((entry, _vocab_pattern(entry)))
        all_vocab_patterns = AI_TELL_HARD_PATTERNS + extra_patterns

        for i, line in enumerate(lines, 1):
            seen_on_line: set[str] = set()
            for term, pat in all_vocab_patterns:
                if term in seen_on_line:
                    continue
                if pat.search(line):
                    seen_on_line.add(term)
                    fix_msg = extra_labels.get(term, f"Replace AI-tell word/phrase: \"{term}\"")
                    findings.append(Finding(
                        severity="HARD",
                        rule="ai_tell_vocab",
                        line=i,
                        match=term,
                        fix=fix_msg,
                    ))

    # Not-X-it's-Y scaffolding — HARD
    if "not_x_its_y" not in skip:
        # Search across the full text with line-number tracking
        for i, line in enumerate(lines, 1):
            for m in NOT_X_ITS_Y_PATTERN.finditer(line):
                findings.append(Finding(
                    severity="HARD",
                    rule="not_x_its_y",
                    line=i,
                    match=m.group(0).strip(),
                    fix="Rewrite: remove 'not X, it's Y' scaffolding pattern",
                ))

    # Soft vocabulary — REVIEW
    if "soft_vocab" not in skip:
        for i, line in enumerate(lines, 1):
            seen_on_line: set[str] = set()
            for term, pat in SOFT_VOCAB_PATTERNS:
                if term in seen_on_line:
                    continue
                if pat.search(line):
                    seen_on_line.add(term)
                    findings.append(Finding(
                        severity="REVIEW",
                        rule="soft_vocab",
                        line=i,
                        match=term,
                        fix=f"Consider replacing soft word: \"{term}\"",
                    ))

    # ------------------------------------------------------------------
    # PLATFORM RULES
    # ------------------------------------------------------------------

    if platform == "linkedin":

        # External links — HARD
        if "linkedin_external_links" not in skip:
            for i, line in enumerate(lines, 1):
                for m in EXTERNAL_LINK_PATTERN.finditer(line):
                    findings.append(Finding(
                        severity="HARD",
                        rule="linkedin_external_links",
                        line=i,
                        match=m.group(0),
                        fix="Remove external link from body (LinkedIn suppresses reach ~60%); put in first comment instead",
                    ))

        # Staircase detection
        if "linkedin_staircase" not in skip:
            # >4 short mono-line blocks total
            if stats.short_line_count > 4:
                findings.append(Finding(
                    severity="HARD",
                    rule="linkedin_staircase",
                    line=0,
                    match=f"{stats.short_line_count} short lines",
                    fix=f"Too many staircase lines ({stats.short_line_count} > 4); combine into multi-sentence paragraphs",
                ))

            # 3+ consecutive short lines
            consec = 0
            max_consec = 0
            for ln in lines:
                if _is_short_line(ln):
                    consec += 1
                    max_consec = max(max_consec, consec)
                else:
                    consec = 0
            if max_consec >= 3:
                findings.append(Finding(
                    severity="HARD",
                    rule="linkedin_staircase_consecutive",
                    line=0,
                    match=f"{max_consec} consecutive short lines",
                    fix=f"Found {max_consec} consecutive staircase lines; merge or expand",
                ))

            # <60% multi-sentence blocks — REVIEW
            if stats.para_count > 0:
                ratio = multi_sentence / stats.para_count
                if ratio < 0.60:
                    findings.append(Finding(
                        severity="REVIEW",
                        rule="linkedin_staircase_ratio",
                        line=0,
                        match=f"{multi_sentence}/{stats.para_count} multi-sentence blocks ({ratio:.0%})",
                        fix="Fewer than 60% of paragraphs are multi-sentence; expand thin blocks",
                    ))

        # Paragraph count — REVIEW if outside 5–8
        if "linkedin_para_count" not in skip:
            if stats.para_count < 5 or stats.para_count > 8:
                findings.append(Finding(
                    severity="REVIEW",
                    rule="linkedin_para_count",
                    line=0,
                    match=f"{stats.para_count} paragraphs",
                    fix=f"Target 5-8 paragraphs for LinkedIn (found {stats.para_count})",
                ))

    # X platform: no external link ban, no staircase, no para-count enforcement
    # (nothing extra to add beyond base rules)

    # ------------------------------------------------------------------
    # CLIENT-SPECIFIC RULES
    # ------------------------------------------------------------------

    # do_not_say_hard: regex patterns — accepts {"pattern":..., "label":...} or plain string
    for entry in (config.get("do_not_say_hard") or []):
        if isinstance(entry, str):
            pat_str, label = entry, entry
        else:
            pat_str = entry.get("pattern", "")
            label = entry.get("label", pat_str)
        if not pat_str:
            continue
        try:
            pat = re.compile(pat_str, re.IGNORECASE)
        except re.error:
            continue
        for i, line in enumerate(lines, 1):
            for m in pat.finditer(line):
                findings.append(Finding(
                    severity="HARD",
                    rule="do_not_say",
                    line=i,
                    match=m.group(0),
                    fix=f"Banned phrase: {label}",
                ))

    # product_names_review: flag if any appear in text — accepts {"pattern":..., "label":...} or plain string
    for name in (config.get("product_names_review") or []):
        if isinstance(name, dict):
            raw = name.get("pattern") or re.escape(name.get("name", ""))
            fix_msg = name.get("label", f"Verify product name: {raw}")
            try:
                pat = re.compile(raw, re.IGNORECASE)
            except re.error:
                continue
        else:
            pat = re.compile(re.escape(name), re.IGNORECASE)
            fix_msg = f"Verify product name spelling/capitalisation: \"{name}\""
        for i, line in enumerate(lines, 1):
            if pat.search(line):
                findings.append(Finding(
                    severity="REVIEW",
                    rule="product_name",
                    line=i,
                    match=pat.pattern,
                    fix=fix_msg,
                ))

    # lane_gates_review: flag if any listed person appears — accepts {"pattern":..., "label":...} or plain string
    for person in (config.get("lane_gates_review") or []):
        if isinstance(person, dict):
            raw = person.get("pattern") or re.escape(person.get("name", ""))
            fix_msg = person.get("label", f"Lane gate: check this mention")
            try:
                pat = re.compile(raw, re.IGNORECASE)
            except re.error:
                continue
        else:
            pat = re.compile(re.escape(person), re.IGNORECASE)
            fix_msg = f"Lane gate: check that mentioning \"{person}\" is intentional and cleared"
        for i, line in enumerate(lines, 1):
            if pat.search(line):
                findings.append(Finding(
                    severity="REVIEW",
                    rule="lane_gate",
                    line=i,
                    match=pat.pattern,
                    fix=fix_msg,
                ))

    # Deduplicate: same rule + line + match (can happen with multi-sentence blocks)
    seen: set[tuple] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.severity, f.rule, f.line, f.match)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    return deduped, stats


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_human(findings: list[Finding], stats: Stats) -> str:
    hard = [f for f in findings if f.severity == "HARD"]
    review = [f for f in findings if f.severity == "REVIEW"]

    lines: list[str] = []

    if hard:
        lines.append(f"❌ LINT FAIL — {len(hard)} hard, {len(review)} review")
    else:
        lines.append(f"✅ LINT PASS — 0 hard, {len(review)} review")

    if hard:
        lines.append("")
        lines.append("HARD (veto — must fix before ship):")
        for f in hard:
            loc = f"L{f.line}" if f.line > 0 else "doc"
            lines.append(f"  [{f.rule}] {loc}: «{f.match}» → {f.fix}")

    if review:
        lines.append("")
        lines.append("REVIEW (for critic panel):")
        for f in review:
            loc = f"L{f.line}" if f.line > 0 else "doc"
            lines.append(f"  [{f.rule}] {loc}: «{f.match}» → {f.fix}")

    lines.append("")
    total = stats.para_count
    multi = stats.multi_sentence_count
    short = stats.short_line_count
    lines.append(f"stats: {total} paras · {multi}/{total} multi-sentence · {short} short")

    return "\n".join(lines)


def format_json(findings: list[Finding], stats: Stats) -> str:
    hard = [f for f in findings if f.severity == "HARD"]
    review = [f for f in findings if f.severity == "REVIEW"]

    def finding_dict(f: Finding) -> dict:
        return {
            "severity": f.severity,
            "rule": f.rule,
            "line": f.line,
            "match": f.match,
            "fix": f.fix,
        }

    payload = {
        "pass": len(hard) == 0,
        "hard": [finding_dict(f) for f in hard],
        "review": [finding_dict(f) for f in review],
        "stats": {
            "para_count": stats.para_count,
            "multi_sentence_count": stats.multi_sentence_count,
            "short_line_count": stats.short_line_count,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic quality gate linter for client posts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to a lint-config.json file with per-client rules",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output structured JSON instead of human-readable text",
    )
    parser.add_argument(
        "file",
        nargs="?",
        metavar="FILE",
        help="Post text file to lint (reads stdin if omitted)",
    )
    args = parser.parse_args()

    # Read input
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(2)
        text = sys.stdin.read()

    # Load config
    config = load_config(args.config)

    # Run lint
    findings, stats = lint(text, config)

    # Output
    if args.output_json:
        print(format_json(findings, stats))
    else:
        print(format_human(findings, stats))

    # Exit code
    hard = [f for f in findings if f.severity == "HARD"]
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
