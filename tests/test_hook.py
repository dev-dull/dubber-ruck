"""Offline tests for hooks/hook.py: guards, diff selection, plan discovery, output shape.
dubber-ruck itself is replaced by a fake executable via DUBBER_RUCK_BIN."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "hook.py"

FAKE_BIN = """#!/bin/bash
# fake dubber-ruck: `status --json` reports what FAKE_STATUS says; review/plan echo a canned answer
if [ "$1" = "status" ]; then echo "$FAKE_STATUS"; exit 0; fi
echo "$@" > "$FAKE_CALLS"
cat > "$FAKE_STDIN" 2>/dev/null
echo "## Findings
- [confidence 4/5] [grounded] x \\`y\\` — z. Verify by: w

## Answer
**Verdict:** FIX FIRST
ok"
"""

STATUS_OK = json.dumps({"server": "llama-swap", "preferred": "model-a", "verdict": "idle", "models": [{"model": "model-a", "state": "ready", "busy": False}]})
STATUS_OTHER = json.dumps({"server": "llama-swap", "preferred": "model-a", "verdict": "idle", "models": [{"model": "model-b", "state": "ready", "busy": False}]})
STATUS_PLAIN = json.dumps({"server": "openai-compatible", "preferred": None, "verdict": "unknown", "models": [{"model": "x", "state": None, "busy": None}]})
STATUS_NOPREF = json.dumps({"server": "llama-swap", "preferred": None, "verdict": "idle", "models": [{"model": "model-b", "state": "ready", "busy": False}]})
STATUS_BUSY = json.dumps({"server": "llama-swap", "preferred": "model-a", "verdict": "busy", "models": [{"model": "model-a", "state": "ready", "busy": True}]})


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".claude" / "plans").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text("{}")
        self.bin = Path(self.tmp.name) / "fake-dubber-ruck"
        self.bin.write_text(FAKE_BIN)
        self.bin.chmod(0o755)
        self.calls = Path(self.tmp.name) / "calls"
        self.stdin_capture = Path(self.tmp.name) / "stdin"
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.repo / "a.py").write_text("x = 1\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *a):
        subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)

    def run_hook(self, kind, payload, status=STATUS_OK, env=None):
        e = {**os.environ, "HOME": str(self.home), "DUBBER_RUCK_BIN": str(self.bin), "FAKE_STATUS": status,
             "FAKE_CALLS": str(self.calls), "FAKE_STDIN": str(self.stdin_capture)}
        e.pop("DUBBER_RUCK_COMMIT_HOOK", None)
        e.pop("DUBBER_RUCK_PLAN_HOOK", None)
        e.update(env or {})
        p = subprocess.run([sys.executable, str(HOOK), kind], input=json.dumps(payload), capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def stage_big_change(self):
        (self.repo / "a.py").write_text("".join(f"line{i} = {i}\n" for i in range(20)))
        self._git("add", "-A")

    # ---- commit

    def test_commit_reviews_staged_diff_quick_by_default(self):
        self.stage_big_change()
        out = self.run_hook("commit", {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("git diff --cached", ctx)
        self.assertIn("[confidence 4/5]", ctx)
        self.assertIn("hypothesis", ctx)
        self.assertIn("--no-think", self.calls.read_text())
        self.assertIn("line19 = 19", self.stdin_capture.read_text())

    def test_commit_think_mode_via_env(self):
        self.stage_big_change()
        self.run_hook("commit", {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)}, env={"DUBBER_RUCK_COMMIT_HOOK": "think"})
        self.assertNotIn("--no-think", self.calls.read_text())

    def test_commit_dash_a_reviews_working_tree(self):
        (self.repo / "a.py").write_text("".join(f"line{i} = {i}\n" for i in range(20)))  # not staged
        out = self.run_hook("commit", {"tool_input": {"command": "git commit -am x"}, "cwd": str(self.repo)})
        self.assertIn("git diff HEAD", out["hookSpecificOutput"]["additionalContext"])

    def test_commit_honours_cd_prefix_and_git_dash_C(self):
        # The hook payload's cwd is the session's; the command may target another repo.
        self.stage_big_change()
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        out = self.run_hook("commit", {"tool_input": {"command": f"cd {self.repo} && git commit -m x"}, "cwd": str(elsewhere)})
        self.assertIsNotNone(out)
        self.assertIn("line19 = 19", self.stdin_capture.read_text())
        self.stdin_capture.unlink()
        (self.home / ".claude" / "dubber-ruck-hook-cache.json").unlink()
        out = self.run_hook("commit", {"tool_input": {"command": f"git -C '{self.repo}' commit -m x"}, "cwd": str(elsewhere)})
        self.assertIsNotNone(out)
        self.assertIn("line19 = 19", self.stdin_capture.read_text())

    def test_prose_in_commit_message_does_not_change_directory(self):
        self.stage_big_change()
        cmd = f"cd {self.repo} && git commit -q -F - <<'EOF'\nHonours `cd DIR &&` and `git -C DIR`, and reviews the tree.\nEOF"
        out = self.run_hook("commit", {"tool_input": {"command": cmd}, "cwd": "/nonexistent"})
        self.assertIsNotNone(out)
        self.assertIn("line19 = 19", self.stdin_capture.read_text())

    def test_commit_after_git_add_in_same_command_reviews_working_tree(self):
        # PreToolUse runs before the command, so `git add && git commit` has nothing staged yet.
        (self.repo / "a.py").write_text("".join(f"line{i} = {i}\n" for i in range(20)))  # not staged
        out = self.run_hook("commit", {"tool_input": {"command": "git add -A && git commit -m x"}, "cwd": str(self.repo)})
        self.assertIsNotNone(out)
        self.assertIn("git diff HEAD", out["hookSpecificOutput"]["additionalContext"])

    def test_commit_skips_trivial_diff(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self._git("add", "-A")
        self.assertIsNone(self.run_hook("commit", {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)}))

    def test_commit_skips_when_other_model_loaded_or_busy_or_off(self):
        self.stage_big_change()
        payload = {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)}
        self.assertIsNone(self.run_hook("commit", payload, status=STATUS_OTHER))
        self.assertIsNone(self.run_hook("commit", payload, status=STATUS_BUSY))
        self.assertIsNone(self.run_hook("commit", payload, env={"DUBBER_RUCK_COMMIT_HOOK": "off"}))
        self.assertFalse(self.calls.exists())

    def test_commit_runs_on_plain_server_and_without_preference(self):
        self.stage_big_change()
        payload = {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)}
        self.assertIsNotNone(self.run_hook("commit", payload, status=STATUS_PLAIN))
        (self.home / ".claude" / "dubber-ruck-hook-cache.json").unlink()
        self.assertIsNotNone(self.run_hook("commit", payload, status=STATUS_NOPREF))

    def test_commit_dedups_same_diff_within_the_hour(self):
        self.stage_big_change()
        payload = {"tool_input": {"command": "git commit -m x"}, "cwd": str(self.repo)}
        self.assertIsNotNone(self.run_hook("commit", payload))
        self.calls.unlink()
        self.assertIsNone(self.run_hook("commit", payload))
        self.assertFalse(self.calls.exists())

    def test_commit_detection_regex(self):
        sys.path.insert(0, str(HOOK.parent))
        import importlib
        hook = importlib.import_module("hook")
        rx = hook.re  # ensure module loaded
        self.assertIsNotNone(rx)
        self.stage_big_change()
        for cmd, expect in [
            ("git commit -m x", True),
            ("git -c user.email=a@b commit -qm x", True),
            (f"cd {self.repo} && git add -A && git commit -m 'x'", True),
            ("cd /tmp && git commit -m 'x'", False),  # /tmp is not a repo: nothing to review
            ("git status && git commit --amend --no-edit", True),
            ("if true; then git commit -m x; fi", True),
            ("git --no-pager commit -m x", True),
            ("git -C /tmp/nonexistent-dir-xyz commit -m x", False),  # directory missing -> skip
            ("git status", False),
            ("git log --oneline | grep commit", False),
            ("echo 'do not git commit yet'", False),
        ]:
            if self.calls.exists():
                self.calls.unlink()
            CACHE = self.home / ".claude" / "dubber-ruck-hook-cache.json"
            if CACHE.exists():
                CACHE.unlink()
            out = self.run_hook("commit", {"tool_input": {"command": cmd}, "cwd": str(self.repo)})
            self.assertEqual(out is not None, expect, cmd)

    def test_commit_never_fails_on_garbage(self):
        p = subprocess.run([sys.executable, str(HOOK), "commit"], input="not json", capture_output=True, text=True,
                           env={**os.environ, "HOME": str(self.home), "DUBBER_RUCK_BIN": "/nonexistent"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    # ---- plan

    def test_plan_checks_newest_recent_plan_with_thinking(self):
        old = self.home / ".claude" / "plans" / "old.md"
        old.write_text("# old plan\n" + "step\n" * 60)
        os.utime(old, (time.time() - 10 * 3600, time.time() - 10 * 3600))
        new = self.home / ".claude" / "plans" / "new.md"
        new.write_text("# new plan\n" + "1. do the thing carefully and verify it\n" * 10)
        out = self.run_hook("plan", {"tool_input": {}, "cwd": str(self.repo)})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new.md", ctx)
        self.assertIn("READY means no concern found", ctx)
        calls = self.calls.read_text()
        self.assertIn("plan", calls)
        self.assertIn("new.md", calls)
        self.assertNotIn("--no-think", calls)

    def test_plan_skips_when_only_stale_plans_exist(self):
        old = self.home / ".claude" / "plans" / "old.md"
        old.write_text("# old plan\n" + "step\n" * 60)
        os.utime(old, (time.time() - 10 * 3600, time.time() - 10 * 3600))
        self.assertIsNone(self.run_hook("plan", {"tool_input": {}, "cwd": str(self.repo)}))

    def test_plan_quick_mode_and_dedup(self):
        new = self.home / ".claude" / "plans" / "new.md"
        new.write_text("# new plan\n" + "1. do the thing carefully and verify it\n" * 10)
        self.assertIsNotNone(self.run_hook("plan", {"tool_input": {}, "cwd": str(self.repo)}, env={"DUBBER_RUCK_PLAN_HOOK": "quick"}))
        self.assertIn("--no-think", self.calls.read_text())
        self.calls.unlink()
        self.assertIsNone(self.run_hook("plan", {"tool_input": {}, "cwd": str(self.repo)}))


if __name__ == "__main__":
    unittest.main()
