#!/usr/bin/env python3
"""Claude Code PreToolUse hooks for dubber ruck.

    python3 hook.py commit   # matcher Bash, if "Bash(git *)"
    python3 hook.py plan     # matcher ExitPlanMode

Reads the hook JSON on stdin, runs dubber-ruck when the checkpoint is worth it, and
prints a JSON object whose hookSpecificOutput.additionalContext carries the review.
Never blocks the tool call, never exits non-zero: on any problem it prints nothing.

Modes, by environment variable (default in bold):
    DUBBER_RUCK_COMMIT_HOOK = off | **quick** | think
    DUBBER_RUCK_PLAN_HOOK   = off | quick | **think**
"quick" is thinking off (about 30-60 s); "think" is thinking on (2-5 min).

Skips, silently: trivial diffs, a server that is busy or unreachable, a loaded model
other than the preferred one (its reviews are not worth the wait), a session that is
itself running on the local model, and anything already reviewed in the last hour.
Every decision is appended to ~/.claude/dubber-ruck-hook.log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("DUBBER_RUCK_CONFIG") or HOME / ".config" / "dubber-ruck" / "config")


def _config() -> dict[str, str]:
    """Same file and precedence as the CLI: config file, then DUBBER_RUCK_* env vars."""
    out: dict[str, str] = {}
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip().startswith("DUBBER_RUCK_"):
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    out.update({k: v for k, v in os.environ.items() if k.startswith("DUBBER_RUCK_")})
    return out


CONFIG = _config()
LOG = HOME / ".claude" / "dubber-ruck-hook.log"
CACHE = HOME / ".claude" / "dubber-ruck-hook-cache.json"
DEDUP_SECONDS = 3600
MIN_DIFF_LINES = int(CONFIG.get("DUBBER_RUCK_HOOK_MIN_LINES", "8"))
PLAN_MAX_AGE = 3 * 3600
BIN = CONFIG.get("DUBBER_RUCK_BIN") or str(HOME / "bin" / "dubber-ruck")
CLI_TIMEOUT = {"quick": 240, "think": 900}


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def emit(context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}))


def run(argv: list[str], *, cwd: str | None = None, timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, cwd=cwd, timeout=timeout, input=stdin)


# --------------------------------------------------------------------------- guards


def mode_for(kind: str) -> str:
    default = "quick" if kind == "commit" else "think"
    value = CONFIG.get(f"DUBBER_RUCK_{kind.upper()}_HOOK", default).strip().lower()
    return value if value in ("off", "quick", "think") else default


def server_ok() -> tuple[bool, str]:
    """True when a usable model is loaded and its slot is idle. On llama-swap that
    means the preferred model (if one is configured) is the resident one; on a plain
    OpenAI-compatible server nothing is visible, so it is assumed usable."""
    try:
        p = run([BIN, "status", "--json"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"status failed: {e}"
    if p.returncode == 2 or not p.stdout.strip():
        return False, "unreachable"
    try:
        info = json.loads(p.stdout)
    except json.JSONDecodeError:
        return False, "status output not JSON"
    if info.get("server") != "llama-swap":
        return True, "ok (plain server, state not visible)"
    preferred = info.get("preferred")
    loaded = [m for m in info.get("models", []) if m.get("state") in ("ready", "starting")]
    if not loaded:
        return False, "no model loaded (cold start would take minutes)"
    if preferred and loaded[0].get("model") != preferred:
        return False, f"loaded model is {loaded[0].get('model')}, not the preferred {preferred}; skipping rather than review with a model the prompts were not calibrated for"
    if info.get("verdict") == "busy":
        return False, "slot busy"
    return True, "ok"


def seen_recently(key: str) -> bool:
    try:
        data = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if now - v < DEDUP_SECONDS}
    hit = key in data
    data[key] = now
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except OSError:
        pass
    return hit


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- commit


# `git commit` at a command position (start, after ; & | ( or then/do), allowing
# global options with or without values: `git -c k=v commit`, `git -C dir commit`,
# `git --no-pager commit`. Not inside quoted prose such as "do not git commit yet".
_GIT_OPTS = r"(?:-{1,2}[\w-]+(?:=\S+|\s+[^-\s]\S*)?\s+)*"
COMMIT_RE = re.compile(r"(^|[;&|(]\s*|\b(?:then|do)\s+)git\s+" + _GIT_OPTS + r"commit\b")
GIT_ADD_RE = re.compile(r"(^|[;&|(]\s*|\b(?:then|do)\s+)git\s+" + _GIT_OPTS + r"add\b")
CD_RE = re.compile(r"^\s*cd\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))\s*(?:&&|;)")
# `-C DIR` only counts when it belongs to the git invocation that runs `commit`;
# a commit message body (heredoc) may mention "git -C DIR" as prose.
GIT_C_COMMIT_RE = re.compile(
    r"(?:^|[;&|(]\s*|\b(?:then|do)\s+)git\s+" + _GIT_OPTS + r"-C\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))\s+" + _GIT_OPTS + r"commit\b"
)


def effective_cwd(command: str, cwd: str) -> str:
    """The directory the commit will run in. The hook payload carries the session's
    cwd, but the command may start with `cd DIR &&` or use `git -C DIR commit`."""
    target = None
    m = GIT_C_COMMIT_RE.search(command)
    if m:
        target = next(g for g in m.groups() if g)
    else:
        m = CD_RE.match(command)
        if m:
            target = next(g for g in m.groups() if g)
    if not target:
        return cwd
    target = os.path.expanduser(os.path.expandvars(target))
    return target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd, target))


def commit_diff(command: str, cwd: str) -> tuple[str, str]:
    """(source label, diff). `git commit -a` commits tracked changes too, so review those.
    A command that runs `git add` before the commit has not staged anything yet when
    this hook runs (PreToolUse fires before the Bash command), so review the working
    tree in that case as the best approximation of what will be committed."""
    all_tracked = bool(re.search(r"(^|\s)-[a-zA-Z]*a[a-zA-Z]*(\s|$)|--all(\s|$)", command)) or bool(GIT_ADD_RE.search(command))
    argv = ["git", "diff", "-U5", "HEAD"] if all_tracked else ["git", "diff", "--cached", "-U5"]
    try:
        p = run(argv, cwd=cwd, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    return ("git diff HEAD" if all_tracked else "git diff --cached"), (p.stdout if p.returncode == 0 else "")


def changed_lines(diff: str) -> int:
    return sum(1 for ln in diff.splitlines() if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")))


def hook_commit(payload: dict) -> None:
    mode = mode_for("commit")
    if mode == "off":
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    # The settings filter is the broad "Bash(git *)" so that `git -c k=v commit` and
    # `cd x && git commit` both arrive here; this is the precise check.
    if not COMMIT_RE.search(command):
        return
    cwd = effective_cwd(command, payload.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        log(f"commit: directory {cwd} does not exist, skipping")
        return
    if "--no-verify" in command:
        log("commit: --no-verify, skipping")
        return
    label, diff = commit_diff(command, cwd)
    n = changed_lines(diff)
    if n < MIN_DIFF_LINES:
        log(f"commit: {n} changed lines < {MIN_DIFF_LINES}, skipping")
        return
    key = "commit:" + digest(diff)
    if seen_recently(key):
        log("commit: same diff reviewed within the hour, skipping")
        return
    ok, why = server_ok()
    if not ok:
        log(f"commit: {why}, skipping")
        return
    argv = [BIN, "review", "--stdin", "-q", "--focus", "This diff is about to be committed. Defects only; skip style."]
    if mode == "quick":
        argv.append("--no-think")
    log(f"commit: reviewing {n} changed lines ({label}), mode {mode}")
    try:
        p = run(argv, cwd=cwd, timeout=CLI_TIMEOUT[mode], stdin=diff)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"commit: dubber-ruck failed: {e}")
        return
    if p.returncode != 0 or not p.stdout.strip():
        log(f"commit: dubber-ruck exit {p.returncode}: {p.stderr.strip()[:200]}")
        return
    log(f"commit: done in mode {mode}")
    emit(
        "dubber ruck (a self-hosted second-opinion model, imperfect by design, run automatically by a pre-commit hook) "
        f"reviewed the {label} that this commit will include:\n\n{p.stdout.strip()}\n\n"
        "The commit proceeds regardless. Treat each finding as a hypothesis: verify it against the code "
        "before acting, discard UNGROUNDED ones unless independently confirmed, and if something real "
        "survives verification, fix it and commit again. Mention what the duck flagged and what survived "
        "in your next message to the user, attributed by name."
    )


# --------------------------------------------------------------------------- plan


def plans_dir(cwd: str) -> Path:
    try:
        settings = json.loads((HOME / ".claude" / "settings.json").read_text())
        custom = settings.get("plansDirectory")
        if custom:
            return Path(cwd) / custom
    except (OSError, json.JSONDecodeError):
        pass
    return HOME / ".claude" / "plans"


def newest_plan(directory: Path, max_age: int = PLAN_MAX_AGE) -> Path | None:
    try:
        candidates = [p for p in directory.glob("*.md") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if time.time() - newest.stat().st_mtime > max_age:
        return None
    return newest


def hook_plan(payload: dict) -> None:
    mode = mode_for("plan")
    if mode == "off":
        return
    cwd = payload.get("cwd") or os.getcwd()
    plan = newest_plan(plans_dir(cwd))
    if plan is None:
        log("plan: no recent plan file found, skipping")
        return
    text = plan.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 200:
        log(f"plan: {plan.name} is too short to check, skipping")
        return
    key = "plan:" + digest(text)
    if seen_recently(key):
        log(f"plan: {plan.name} unchanged since last check, skipping")
        return
    ok, why = server_ok()
    if not ok:
        log(f"plan: {why}, skipping")
        return
    argv = [BIN, "plan", str(plan), "-q"]
    if mode == "quick":
        argv.append("--no-think")
    log(f"plan: checking {plan.name} ({len(text)} chars), mode {mode}")
    try:
        p = run(argv, cwd=cwd, timeout=CLI_TIMEOUT[mode])
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"plan: dubber-ruck failed: {e}")
        return
    if p.returncode != 0 or not p.stdout.strip():
        log(f"plan: dubber-ruck exit {p.returncode}: {p.stderr.strip()[:200]}")
        return
    log(f"plan: done in mode {mode}")
    emit(
        "dubber ruck (a self-hosted second-opinion model, imperfect by design, run automatically when a plan is presented) "
        f"checked the plan file {plan}:\n\n{p.stdout.strip()}\n\n"
        "The verdict was computed by the tool from fixed questions, so READY means no concern found, not "
        "approval. For each concern or unanswered question, decide whether it is real: if it is, revise "
        "the plan file before the user reviews it, or say in the plan what you decided and why. Mention "
        "the check to the user in one or two sentences, attributed by name."
    )


# --------------------------------------------------------------------------- main


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        if kind == "commit":
            hook_commit(payload)
        elif kind == "plan":
            hook_plan(payload)
        else:
            log(f"unknown hook kind {kind!r}")
    except Exception as e:  # noqa: BLE001 - a hook must never break the tool call
        log(f"{kind}: unexpected error: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
