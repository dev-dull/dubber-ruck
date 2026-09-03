"""Offline tests for the pure parts of dubber_ruck. Run: python3 -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ["DUBBER_RUCK_CONFIG"] = "/nonexistent/dubber-ruck-config"
for _k in [k for k in os.environ if k.startswith("DUBBER_RUCK_") and k != "DUBBER_RUCK_CONFIG"]:
    del os.environ[_k]
import dubber_ruck as dr  # noqa: E402

# Configured-model map as /v1/models reports it. The 'running' dict passed to
# choose_model is the authority on what is resident; these status strings are not.
CONFIGURED = {"model-a": "unknown", "model-b": "unknown"}


class ChooseModel(unittest.TestCase):
    def test_uses_loaded_model_when_nothing_requested(self):
        model, note = dr.choose_model(CONFIGURED, {"model-a": "ready"}, None, "model-a")
        self.assertEqual(model, "model-a")
        self.assertIsNone(note)

    def test_uses_loaded_model_even_if_not_preferred(self):
        model, note = dr.choose_model(CONFIGURED, {"model-b": "ready"}, None, "model-a")
        self.assertEqual(model, "model-b")
        self.assertTrue(note.startswith("WARNING"), note)
        self.assertIn("not the preferred model-a", note)

    def test_refuses_to_swap_without_flag(self):
        with self.assertRaises(dr.Refused):
            dr.choose_model(CONFIGURED, {"model-b": "ready"}, "model-a", "model-a")

    def test_swaps_with_flag(self):
        model, note = dr.choose_model(CONFIGURED, {"model-b": "ready"}, "model-a", "model-a", allow_swap=True)
        self.assertEqual(model, "model-a")
        self.assertIn("swapping", note)

    def test_requested_model_already_loaded_is_silent(self):
        model, note = dr.choose_model(CONFIGURED, {"model-a": "ready"}, "model-a", "model-a")
        self.assertEqual(model, "model-a")
        self.assertIsNone(note)

    def test_nothing_loaded_uses_preferred_with_cold_start_note(self):
        model, note = dr.choose_model(CONFIGURED, {}, None, "model-a")
        self.assertEqual(model, "model-a")
        self.assertIn("cold start", note)

    def test_starting_counts_as_resident(self):
        with self.assertRaises(dr.Refused):
            dr.choose_model(CONFIGURED, {"model-b": "starting"}, "model-a", "model-a")

    def test_unknown_model_rejected(self):
        with self.assertRaises(dr.DuckError):
            dr.choose_model(CONFIGURED, {}, "coder", "model-a")

    def test_unknown_preferred_rejected(self):
        with self.assertRaises(dr.DuckError):
            dr.choose_model(CONFIGURED, {}, None, "coder")

    # No preferred model configured: use whatever is loaded.
    def test_no_preference_takes_the_loaded_model_silently(self):
        model, note = dr.choose_model(CONFIGURED, {"model-b": "ready"}, None, None)
        self.assertEqual(model, "model-b")
        self.assertIsNone(note)

    def test_no_preference_nothing_loaded_single_model_cold_starts(self):
        model, note = dr.choose_model({"only": "unknown"}, {}, None, None)
        self.assertEqual(model, "only")
        self.assertIn("cold start", note)

    def test_no_preference_nothing_loaded_many_models_needs_a_choice(self):
        with self.assertRaises(dr.DuckError):
            dr.choose_model(CONFIGURED, {}, None, None)

    # Plain OpenAI-compatible server: llama-swap's process list is None.
    def test_plain_server_uses_requested_or_preferred(self):
        self.assertEqual(dr.choose_model(CONFIGURED, None, "model-b", "model-a"), ("model-b", None))
        self.assertEqual(dr.choose_model(CONFIGURED, None, None, "model-a"), ("model-a", None))

    def test_plain_server_single_model_needs_no_config(self):
        self.assertEqual(dr.choose_model({"only": "unknown"}, None, None, None), ("only", None))

    def test_plain_server_many_models_no_preference_is_an_error(self):
        with self.assertRaises(dr.DuckError):
            dr.choose_model(CONFIGURED, None, None, None)

    def test_plain_server_preferred_not_offered_is_an_error(self):
        with self.assertRaises(dr.DuckError):
            dr.choose_model(CONFIGURED, None, None, "coder")

    def test_plain_server_never_refuses_a_swap(self):
        self.assertEqual(dr.choose_model(CONFIGURED, None, "model-b", "model-a"), ("model-b", None))


class Config(unittest.TestCase):
    def test_file_then_env_precedence(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("# comment\nDUBBER_RUCK_URL = 'http://file.example:1'\nDUBBER_RUCK_MODEL=\"m\"\nOTHER=ignored\n")
            path = Path(f.name)
        try:
            os.environ["DUBBER_RUCK_MODEL"] = "from-env"
            c = dr.load_config(path)
        finally:
            del os.environ["DUBBER_RUCK_MODEL"]
            path.unlink()
        self.assertEqual(c["DUBBER_RUCK_URL"], "http://file.example:1")
        self.assertEqual(c["DUBBER_RUCK_MODEL"], "from-env")
        self.assertNotIn("OTHER", c)

    def test_missing_file_is_fine(self):
        self.assertEqual({k: v for k, v in dr.load_config(Path("/nonexistent/x")).items() if k != "DUBBER_RUCK_CONFIG"}, {})


class Estimates(unittest.TestCase):
    def test_token_estimate_matches_measured_ratio(self):
        # 16811 chars measured at 4740 tokens on the author's server.
        est = dr.estimate_tokens("x" * 16811)
        self.assertTrue(4300 <= est <= 5200, est)

    def test_seconds_thinking_is_minutes(self):
        self.assertGreater(dr.estimate_seconds(4700, think=True), 100)
        self.assertLess(dr.estimate_seconds(4700, think=False), 60)

    def test_fmt_duration(self):
        self.assertEqual(dr.fmt_duration(30), "30s")
        self.assertEqual(dr.fmt_duration(150), "2.5 min")


class Messages(unittest.TestCase):
    def test_attachments_precede_question(self):
        msg = dr.build_user_message("why?", [("a.py", "print(1)\n")])
        self.assertLess(msg.index("### File: a.py"), msg.index("### Question"))
        self.assertIn("```\nprint(1)\n```", msg)

    def test_fence_widens_when_content_has_backticks(self):
        msg = dr.build_user_message("q", [("doc.md", "```py\nx\n```")])
        self.assertIn("````\n```py", msg)


class Footer(unittest.TestCase):
    def test_footer_mentions_model_and_reminder(self):
        res = dr.Result(content="x", reasoning="", finish_reason="stop", model="unsloth/model-a-A3B-GGUF:UD-Q4_K_M", wall=12.0, prompt_tokens=100, completion_tokens=50)
        text = dr.footer(res, think=False, note="hello")
        self.assertIn("model-a-A3B-GGUF:UD-Q4_K_M", text)
        self.assertIn("thinking off", text)
        self.assertIn("note: hello", text)
        self.assertIn("Verify", text)

    def test_footer_warns_on_truncation(self):
        res = dr.Result(content="x", reasoning="", finish_reason="length", model="m", wall=1, truncated=True)
        self.assertIn("cut off", dr.footer(res, think=True))


def sse(*objs, done=True):
    lines = [b"data: " + __import__("json").dumps(o).encode() + b"\n" for o in objs]
    lines.insert(1, b"\n")  # blank keep-alive line
    lines.insert(0, b": comment line\n")
    if done:
        lines.append(b"data: [DONE]\n")
    return lines


class Stream(unittest.TestCase):
    def test_folds_reasoning_content_usage_and_timings(self):
        st = dr.StreamState()
        lines = sse(
            {"model": "m", "choices": [{"delta": {"reasoning_content": "hmm "}}]},
            {"choices": [{"delta": {"reasoning_content": "ok"}}]},
            {"choices": [{"delta": {"content": "## Findings"}}]},
            {"choices": [{"delta": {"content": "\n- none"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "timings": {"predicted_per_second": 26.0}},
        )
        dr.consume_stream(lines, st, deadline=__import__("time").time() + 60)
        self.assertEqual(st.reasoning, "hmm ok")
        self.assertEqual(st.content, "## Findings\n- none")
        self.assertEqual(st.finish, "stop")
        self.assertEqual(st.model, "m")
        self.assertEqual(st.usage["completion_tokens"], 5)
        self.assertEqual(st.timings["predicted_per_second"], 26.0)

    def test_deadline_raises_but_keeps_partial(self):
        st = dr.StreamState()
        lines = sse({"choices": [{"delta": {"reasoning_content": "partial"}}]}, done=False)
        with self.assertRaises(dr.StreamDeadline):
            dr.consume_stream(lines, st, deadline=0)
        self.assertEqual(st.reasoning, "")  # deadline checked before the first line is folded

    def test_garbage_lines_ignored(self):
        st = dr.StreamState()
        dr.consume_stream([b"data: {not json\n", b"event: ping\n", b"data: [DONE]\n"], st, deadline=__import__("time").time() + 60)
        self.assertEqual(st.content, "")

    def test_overall_timeout_scales_with_budget(self):
        self.assertEqual(dr.overall_timeout(1000, 2000, think=False), dr.DEFAULT_TIMEOUT_NOTHINK)
        self.assertGreater(dr.overall_timeout(6000, 16000, think=True), 900)


class Rescue(unittest.TestCase):
    def test_rescue_hands_notes_back_with_thinking_off(self):
        seen = {}

        def fake_chat(server, model, messages, *, think, max_tokens, timeout, **kw):
            seen.update(messages=messages, think=think, max_tokens=max_tokens)
            return dr.Result(content="## Findings\n- none\n## Answer\nfine", reasoning="", finish_reason="stop", model=model, wall=5.0, completion_tokens=40)

        original = dr.chat
        dr.chat = fake_chat
        try:
            cut = dr.Result(content="", reasoning="I was thinking about X and Y", finish_reason="length", model="m", wall=100.0, completion_tokens=8000, truncated=True)
            base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
            res = dr.rescue(dr.Server("http://x"), "m", base, cut, timeout=30)
        finally:
            dr.chat = original

        self.assertFalse(seen["think"])
        self.assertEqual(seen["messages"][:2], base)
        self.assertEqual(seen["messages"][2]["role"], "assistant")
        self.assertIn("thinking about X and Y", seen["messages"][2]["content"])
        self.assertIn("Do not reason further", seen["messages"][3]["content"])
        self.assertEqual(res.wall, 105.0)
        self.assertEqual(res.completion_tokens, 8040)
        self.assertEqual(res.reasoning, cut.reasoning)


class SelfConsult(unittest.TestCase):
    def test_refuses_same_host(self):
        import os
        old = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["ANTHROPIC_BASE_URL"] = "http://llm.example.lan:8080"
        try:
            with self.assertRaises(dr.Refused):
                dr.self_consult_check(dr.Server("http://llm.example.lan:8080"), force=False)
            dr.self_consult_check(dr.Server("http://llm.example.lan:8080"), force=True)
            dr.self_consult_check(dr.Server("http://other:8080"), force=False)
        finally:
            if old is None:
                del os.environ["ANTHROPIC_BASE_URL"]
            else:
                os.environ["ANTHROPIC_BASE_URL"] = old


if __name__ == "__main__":
    unittest.main()
