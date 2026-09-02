"""End-to-end test against a fake llama-swap: exercises the whole consult path offline,
including streaming, grounding annotation, the footer, and the rescue pass."""

import contextlib
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dubber_ruck as dr  # noqa: E402

MODEL = "Qwen3.6-35B"


class FakeSwap(BaseHTTPRequestHandler):
    # Class-level knobs the tests flip.
    busy = False
    reply = "## Findings\n- [confidence 5/5] a.py `x = 1` — fine. Verify by: look\n- [3/5] a.py `y = 2` — invented. Verify by: look\n\n## Answer\nok\n\n## Unsure about\nnothing"
    exhaust_first = False  # first chat call returns only reasoning, finish=length
    calls: list = []

    def log_message(self, *a):  # silence
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"data": [{"id": MODEL, "status": {"value": "loaded"}}, {"id": "Other", "status": {"value": "unloaded"}}]})
        elif self.path == "/running":
            self._json({"running": [{"model": MODEL, "state": "ready"}]})
        elif self.path == f"/upstream/{MODEL}/slots":
            self._json([{"id": 0, "n_ctx": 131072, "is_processing": FakeSwap.busy}])
        else:
            self._json({"error": "nope"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        FakeSwap.calls.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        think = body.get("chat_template_kwargs", {}).get("enable_thinking")
        if FakeSwap.exhaust_first and think:
            FakeSwap.exhaust_first = False
            chunk({"model": MODEL, "choices": [{"delta": {"reasoning_content": "thinking about y = 2 ... "}}]})
            chunk({"choices": [{"delta": {}, "finish_reason": "length"}]})
            chunk({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": body["max_tokens"]}})
        else:
            if think:
                chunk({"model": MODEL, "choices": [{"delta": {"reasoning_content": "hmm"}}]})
            for piece in (FakeSwap.reply[:20], FakeSwap.reply[20:]):
                chunk({"model": MODEL, "choices": [{"delta": {"content": piece}}]})
            chunk({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            chunk({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 40}, "timings": {"prompt_n": 50, "predicted_n": 40, "predicted_per_second": 26.0}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSwap)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.material = Path(__file__).parent / "_fake_material.py"
        cls.material.write_text("x = 1\nz = 3\n")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.material.unlink(missing_ok=True)

    def setUp(self):
        FakeSwap.busy = False
        FakeSwap.exhaust_first = False
        FakeSwap.calls = []

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dr.main(list(argv) + ["--url", self.url])
        return rc, out.getvalue(), err.getvalue()

    def test_consult_end_to_end_with_grounding(self):
        rc, out, err = self.run_cli("consult", "is this fine?", "-f", str(self.material), "-q")
        self.assertEqual(rc, 0, err)
        self.assertIn("[confidence 5/5] [grounded] a.py `x = 1`", out)
        self.assertIn("[confidence 3/5] [UNGROUNDED", out)
        self.assertIn("findings: 2 (1 grounded, 1 UNGROUNDED", out)
        self.assertIn("thinking on", out)
        body = FakeSwap.calls[-1]
        self.assertTrue(body["stream"])
        self.assertTrue(body["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("x = 1", body["messages"][1]["content"])
        self.assertIn("is this fine?", body["messages"][1]["content"])

    def test_no_think_flag_reaches_server(self):
        rc, out, _ = self.run_cli("duck", "why is it slow", "-q", "--raw")
        self.assertEqual(rc, 0)
        self.assertFalse(FakeSwap.calls[-1]["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("dubber ruck ·", out)  # --raw drops the footer

    def test_busy_slot_times_out_with_exit_3(self):
        FakeSwap.busy = True
        rc, _, err = self.run_cli("consult", "q", "--wait", "0.5", "-q")
        self.assertEqual(rc, 3)
        self.assertIn("busy", err)

    def test_rescue_after_reasoning_exhausts_budget(self):
        FakeSwap.exhaust_first = True
        rc, out, err = self.run_cli("consult", "q", "-f", str(self.material))
        self.assertEqual(rc, 0, err)
        self.assertIn("asking for the answer from its notes", err)
        self.assertIn("answer written from cut-off reasoning notes", out)
        self.assertEqual(len(FakeSwap.calls), 2)
        second = FakeSwap.calls[1]
        self.assertFalse(second["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(second["messages"][2]["role"], "assistant")
        self.assertIn("thinking about y = 2", second["messages"][2]["content"])

    def test_no_rescue_flag_exits_5(self):
        FakeSwap.exhaust_first = True
        rc, _, err = self.run_cli("consult", "q", "--no-rescue", "-q")
        self.assertEqual(rc, 5)
        self.assertIn("max_tokens", err)

    def test_votes_make_n_calls_with_distinct_seeds_and_merge(self):
        rc, out, err = self.run_cli("consult", "q", "-f", str(self.material), "--votes", "2", "--seed", "7", "-q")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(FakeSwap.calls), 2)
        self.assertEqual([c["seed"] for c in FakeSwap.calls], [7, 8])
        self.assertEqual(FakeSwap.calls[0]["temperature"], 0.7)
        self.assertIn("[votes 2/2]", out)
        self.assertIn("votes 2: 2 finding(s) kept by majority, 0 dropped", out)
        self.assertIn("runs: ", out)

    def test_plan_mode_renders_code_decided_verdict(self):
        plan = Path(__file__).parent / "_fake_plan.md"
        plan.write_text("1. Delete the old table.\n2. Run the tests.\n")
        old = FakeSwap.reply
        FakeSwap.reply = ("## Q1 unread-files\n**Answer:** NO\n**Evidence:** `Run the tests.` — fine\n\n"
                          "## Q4 no-rollback\n**Answer:** YES\n**Evidence:** `Delete the old table.` — no rollback\n\n"
                          "## Q8 riskiest-step\n**Answer:** step 1\n\n## Unsure about\nnothing\n")
        try:
            rc, out, err = self.run_cli("plan", str(plan), "-q")
        finally:
            FakeSwap.reply = old
            plan.unlink(missing_ok=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("# Plan check: NOT READY: 1 concern(s)", out)
        self.assertIn("- Q4 no-rollback: YES [grounded] `Delete the old table.`", out)
        self.assertIn("| Q2 | unverified-claims | MISSING |", out)
        self.assertIn("Q2 unverified-claims: MISSING", out)
        sysmsg = FakeSwap.calls[-1]["messages"][0]["content"]
        self.assertIn("Q4 no-rollback:", sysmsg)
        self.assertNotIn("{QUESTIONS}", sysmsg)

    def test_status_reports_loaded_and_idle(self):
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("verdict: idle", out)
        self.assertIn("ctx 131072/slot", out)

    def test_swap_refusal(self):
        rc, _, err = self.run_cli("consult", "q", "--model", "Other", "-q")
        self.assertEqual(rc, 4)
        self.assertIn("--allow-swap", err)


if __name__ == "__main__":
    unittest.main()
