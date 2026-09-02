#!/usr/bin/env python3
"""Replay the local_llm_tests pr-review prompt through `dubber-ruck review` and score it.

The benchmark plants two real bugs (PR 3: a spinSpeed field written but never read;
PR 6: a range-loop copy so rain state never advances) and two traps (demanding
rand.Seed on Go 1.22; claiming time.Sleep in a Cmd blocks the UI). This script counts
catches, trap falls, and ungrounded findings per run. Use it after any change to
prompts/review.md. Each run costs about 2-3 minutes of the slot.

Usage: python3 tests/eval/pr_review_eval.py [--runs N] [--prompt PATH] [--no-think]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".dubber-ruck"
RESULTS = ROOT / "tests" / "eval" / "results"
REVIEW_PROMPT = ROOT / "prompts" / "review.md"
PROMPT_URL = "https://raw.githubusercontent.com/dev-dull/local_llm_tests/main/prompts/pr-review/prompt.md"

CHECKS = {
    "PR3 spinSpeed dead": re.compile(r"spinSpeed.{0,160}(never|not|isn't|is not|un)\s*-?(read|used|consumed|applied|wired|referenced)|m\.spun \+= 0\.05.{0,200}spinSpeed|spinSpeed.{0,200}m\.spun \+= 0\.05", re.I | re.S),
    "PR6 range copy": re.compile(r"(range).{0,200}(copy|copies|by value|value semantics)|m\.rainPos\[i\]", re.I | re.S),
}
# Sentence split on a full stop followed by whitespace, so dots inside identifiers
# such as rand.Seed or time.Sleep do not split a sentence.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]


# A sleep-trap fall is an *assertion* that the sleep blocks or stalls the UI, loop, or
# input. "without blocking", "does not block", "non-blocking" are the correct answer.
SLEEP_FALL_RE = re.compile(
    r"sleep.{0,200}?(?<!without )(?<!not )(?<!never )(?<!non-)(?<!no )(?<!doesn't )(?<!won't )"
    r"(block|freez|hang|stall|prevent|starv)\w*.{0,160}"
    r"(ui\b|terminal|input|keypress|key press|event loop|render|message|processing|the loop|responsive)",
    re.I,
)
# A rand.Seed fall is a demand for it without a version hedge. "in older Go versions",
# "before 1.20", "auto-seeds" are the correct caveat the rubric accepts.
SEED_HEDGE_RE = re.compile(r"older|<\s*1\.20|before (go )?1\.20|1\.20\+|since (go )?1\.20|auto-?seed|no longer (needed|required)|not (needed|required)|unnecessary|obsolete", re.I)


def sleep_trap(text: str) -> bool:
    return any(SLEEP_FALL_RE.search(s) for s in sentences(text))


def seed_trap(text: str) -> bool:
    return any("rand.seed" in s.lower() and not SEED_HEDGE_RE.search(s) for s in sentences(text))


TRAPS = {"rand.Seed demanded": seed_trap, "sleep blocks UI": sleep_trap}


def _selfcheck():
    assert sleep_trap("the sleep blocks the terminal.")
    assert sleep_trap("time.Sleep will freeze keyboard input and rendering during the sleep interval.")
    assert sleep_trap("Sleeping here blocks the loop and prevents keypresses from being processed for 33 ms per frame.")
    assert sleep_trap("the sleep runs on the Cmd goroutine and prevents Bubble Tea from processing other messages during the sleep.")
    assert not sleep_trap("the sleep throttles the rate without blocking the UI event loop.")
    assert not sleep_trap("time.Sleep does not block the event loop.")
    assert seed_trap("You must call rand.Seed before using rand.Intn.")
    assert not seed_trap("PR 4 requires an explicit `rand.Seed` call in older Go versions (<1.20), but not here.")
    assert not seed_trap("Go 1.20+ auto-seeds, so rand.Seed is unnecessary.")


_selfcheck()


def fetch_prompt(path: str | None) -> Path:
    if path:
        return Path(path)
    CACHE.mkdir(exist_ok=True)
    dest = CACHE / "pr-review-prompt.md"
    if not dest.exists():
        with urllib.request.urlopen(PROMPT_URL, timeout=30) as r:
            dest.write_bytes(r.read())
    return dest


def strip_file_instructions(text: str) -> str:
    # The benchmark asks the model to write results.md; we want an answer on stdout.
    text = text.replace("**Your deliverable is a file: write your review to `results.md` in the\ncurrent directory. Do not give the review as a chat reply.**", "")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--prompt")
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep each run's output under .dubber-ruck/eval/")
    ap.add_argument("--label", default="", help="short note recorded with the results (what changed)")
    ap.add_argument("--no-record", action="store_true", help="do not write tests/eval/results/<date>-<prompt hash>.md")
    args = ap.parse_args()

    prompt_path = fetch_prompt(args.prompt)
    material = strip_file_instructions(prompt_path.read_text(encoding="utf-8"))
    outdir = CACHE / "eval"
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    for run in range(1, args.runs + 1):
        cmd = [sys.executable, str(ROOT / "dubber_ruck.py"), "review", "--stdin", "-q",
               "--focus", "Review each of the six PRs on its own. Report only defects and clearly better alternatives."]
        if args.no_think:
            cmd.append("--no-think")
        t0 = time.time()
        p = subprocess.run(cmd, input=material, capture_output=True, text=True)
        wall = time.time() - t0
        out = p.stdout
        if p.returncode != 0:
            print(f"run {run}: dubber-ruck exited {p.returncode}: {p.stderr.strip()}", file=sys.stderr)
            rows.append({"run": run, "error": p.stderr.strip(), "seconds": round(wall)})
            continue
        (outdir / f"pr-review-run{run}.md").write_text(out, encoding="utf-8")
        row = {
            "run": run,
            "seconds": round(wall),
            "format_ok": "## Findings" in out and "## Answer" in out,
            "findings": len(re.findall(r"^\s*[-*]\s*\[confidence", out, re.M)),
            "ungrounded": len(re.findall(r"\[UNGROUNDED", out)),
            "unquoted": len(re.findall(r"\[unquoted\]", out)),
        }
        for name, rx in CHECKS.items():
            row[name] = bool(rx.search(out))
        for name, fn in TRAPS.items():
            row[name] = bool(fn(out))
        rows.append(row)
        print(json.dumps(row), flush=True)

    print()
    print("| run | s | format ok | findings | ungrounded | unquoted | PR3 caught | PR6 caught | rand.Seed trap | sleep trap |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| {r['run']} | {r['seconds']} | error | | | | | | | |")
            continue
        yn = lambda k: "yes" if r[k] else "no"  # noqa: E731
        print(f"| {r['run']} | {r['seconds']} | {yn('format_ok')} | {r['findings']} | {r['ungrounded']} | {r['unquoted']} | {yn('PR3 spinSpeed dead')} | {yn('PR6 range copy')} | {yn('rand.Seed demanded')} | {yn('sleep blocks UI')} |")
    ok = [r for r in rows if "error" not in r]
    if ok:
        print(f"\ncatch rate: PR3 {sum(r['PR3 spinSpeed dead'] for r in ok)}/{len(ok)}, PR6 {sum(r['PR6 range copy'] for r in ok)}/{len(ok)}; "
              f"trap falls: {sum(r['rand.Seed demanded'] + r['sleep blocks UI'] for r in ok)}; ungrounded findings: {sum(r['ungrounded'] for r in ok)}")
    print(f"outputs: {outdir}")

    if not args.no_record and ok:
        prompt_hash = hashlib.sha256(REVIEW_PROMPT.read_bytes()).hexdigest()[:10]
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
        record = RESULTS / f"{stamp}-{prompt_hash}.md"
        pr3 = sum(r["PR3 spinSpeed dead"] for r in ok)
        pr6 = sum(r["PR6 range copy"] for r in ok)
        traps = sum(r["rand.Seed demanded"] + r["sleep blocks UI"] for r in ok)
        lines = [
            f"# pr-review replay {stamp}",
            "",
            f"- review prompt: `prompts/review.md` sha256 `{prompt_hash}`",
            f"- mode: thinking {'off' if args.no_think else 'on'}",
            f"- label: {args.label or '(none)'}",
            f"- runs: {len(ok)}; PR3 caught {pr3}/{len(ok)}; PR6 caught {pr6}/{len(ok)}; trap falls {traps}; ungrounded findings {sum(r['ungrounded'] for r in ok)}",
            "",
            "| run | s | format ok | findings | ungrounded | unquoted | PR3 | PR6 | rand.Seed trap | sleep trap |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in ok:
            yn = lambda k: "yes" if r[k] else "no"  # noqa: E731
            lines.append(f"| {r['run']} | {r['seconds']} | {yn('format_ok')} | {r['findings']} | {r['ungrounded']} | {r['unquoted']} | {yn('PR3 spinSpeed dead')} | {yn('PR6 range copy')} | {yn('rand.Seed demanded')} | {yn('sleep blocks UI')} |")
        record.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"recorded: {record.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
