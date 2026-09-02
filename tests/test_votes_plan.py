"""Offline tests for --votes merging and the plan-check parser/verdict."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dubber_ruck as dr  # noqa: E402

MATERIAL = "for i, c := range m.rainPos {\n\tc.y += c.v\n}\nm.spun += 0.05\n"


def mkpass(findings_md: str, verdict: str, unsure: str = "none") -> dr.Pass:
    md = f"## Findings\n{findings_md}\n\n## Answer\n**Verdict:** {verdict}\nBecause.\n\n## Unsure about\n{unsure}\n"
    f = dr.parse_findings(md)
    dr.ground(f, MATERIAL)
    return dr.Pass(dr.Result(content=md, reasoning="", finish_reason="stop", model="m", wall=10.0), dr.annotate(md, f), f, dr.extract_verdict(md))


class Votes(unittest.TestCase):
    def test_majority_keeps_shared_findings_and_drops_singletons(self):
        p1 = mkpass("- [5/5] A `m.spun += 0.05` — dead\n- [3/5] B `c.y += c.v` — copy", "FIX FIRST")
        p2 = mkpass("- [4/5] A `m.spun += 0.05` — dead field\n- [2/5] C `x = invented()` — nope", "FIX FIRST")
        p3 = mkpass("- [5/5] A' `m.spun +=   0.05` — same line, odd spacing", "SHIP", "Go version?")
        md, summary = dr.merge_votes([p1, p2, p3])
        self.assertIn("[votes 3/3]", md)
        self.assertIn("m.spun += 0.05", md.split("## Dropped")[0])
        self.assertIn("## Dropped (minority of runs)", md)
        self.assertIn("[votes 1/3]", md)
        self.assertIn("x = invented()", md.split("## Dropped")[1])
        self.assertIn("**Verdict:** FIX FIRST (2/3 runs; 1 said SHIP)", md)
        self.assertIn("Go version?", md)
        self.assertEqual(summary, "votes 3: 1 finding(s) kept by majority, 2 dropped")

    def test_kept_finding_prefers_grounded_highest_confidence(self):
        p1 = mkpass("- [3/5] A `m.spun += 0.05` — weak wording", "SHIP")
        p2 = mkpass("- [5/5] A `m.spun += 0.05` — strong wording", "SHIP")
        md, _ = dr.merge_votes([p1, p2])
        self.assertIn("[confidence 5/5] [grounded] [votes 2/2]", md)
        self.assertIn("strong wording", md)

    def test_verdict_tie_goes_to_the_cautious_side(self):
        p1 = mkpass("- none", "SHIP")
        p2 = mkpass("- none", "RETHINK")
        md, _ = dr.merge_votes([p1, p2])
        self.assertIn("**Verdict:** RETHINK (1/2 runs; 1 said SHIP)", md)
        self.assertIn("none agreed by a majority", md)

    def test_ungrounded_survives_vote_but_stays_tagged(self):
        p1 = mkpass("- [4/5] Z `not.in.material()` — hmm", "FIX FIRST")
        p2 = mkpass("- [4/5] Z `not.in.material()` — hmm again", "FIX FIRST")
        md, _ = dr.merge_votes([p1, p2])
        self.assertIn("[UNGROUNDED", md)
        self.assertIn("[votes 2/2]", md)


PLAN = """# Plan
1. Delete the old `legacy_sync` table after the migration runs.
2. Assume nobody reads `/v1/export` any more and remove it.
3. Run the unit tests.
"""

REPLY = """## Q1 unread-files
**Answer:** NO
**Evidence:** `Run the unit tests.` — every file touched is named.

## Q2 unverified-claims
**Answer:** YES
**Evidence:** `Assume nobody reads `/v1/export` any more and remove it.` — asserted, never checked.

## Q3 unchecked-assumption
**Answer:** yes.
**Evidence:** `Assume nobody reads` — the assumption is stated as fact.

## Q4 no-rollback
**Answer:** YES
**Evidence:** `Delete the old `legacy_sync` table after the migration runs.` — no rollback step.

## Q5 interface-change
**Answer:** YES
**Evidence:** `remove it` — removes a public endpoint.

## Q6 scope-mismatch
**Answer:** UNCLEAR
**Evidence:** the goal is not stated.

## Q7 simpler-alternative
**Answer:** NO
**Evidence:** `Run the unit tests.` — fine.

## Q8 riskiest-step
**Answer:** Step 1, the table deletion, because `Delete the old` is irreversible.

## Unsure about
Whether a backup exists.
"""


class Plan(unittest.TestCase):
    def test_parse_all_questions_in_order(self):
        a = dr.parse_plan_answers(REPLY)
        self.assertEqual([x.qid for x in a], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual([x.answer for x in a[:7]], ["NO", "YES", "YES", "YES", "YES", "UNCLEAR", "NO"])
        self.assertTrue(a[7].answer.startswith("Step 1"))

    def test_missing_block_is_marked(self):
        a = dr.parse_plan_answers("## Q2 unverified-claims\n**Answer:** NO\n**Evidence:** `x`\n")
        self.assertEqual(a[0].answer, "MISSING")
        self.assertEqual(a[1].answer, "NO")

    def test_verdict_and_grounding(self):
        a = dr.parse_plan_answers(REPLY)
        dr.ground_plan(a, PLAN)
        verdict, concerns, attention, unclear = dr.plan_verdict(a)
        self.assertTrue(verdict.startswith("NOT READY: 3 concern(s)"))
        self.assertEqual([c.key for c in concerns], ["unverified-claims", "unchecked-assumption", "no-rollback"])
        self.assertEqual([c.key for c in attention], ["interface-change"])
        self.assertEqual([c.key for c in unclear], ["scope-mismatch"])
        self.assertTrue(a[3].grounded)  # Q4 quote is in the plan
        self.assertTrue(a[0].grounded)

    def test_evidence_with_plan_backticks_inside_grounds(self):
        plan = "2. Assume nobody reads `/v1/export` any more and remove it.\n"
        reply = "## Q2 unverified-claims\n**Answer:** YES\n**Evidence:** `Assume nobody reads `/v1/export` any more and remove it.` — asserted, never checked.\n"
        a = dr.parse_plan_answers(reply)
        dr.ground_plan(a, plan)
        self.assertTrue(a[1].grounded)

    def test_ready_when_clean(self):
        clean = "\n".join(f"## Q{q} k\n**Answer:** NO\n**Evidence:** `x`\n" for q in range(1, 8)) + "## Q8 k\n**Answer:** none\n"
        verdict, *_ = dr.plan_verdict(dr.parse_plan_answers(clean))
        self.assertTrue(verdict.startswith("READY:"))

    def test_report_shape(self):
        a = dr.parse_plan_answers(REPLY)
        dr.ground_plan(a, PLAN)
        md, summary = dr.render_plan_report(a, "Whether a backup exists.")
        self.assertTrue(md.startswith("# Plan check: NOT READY"))
        self.assertIn("| Q4 | no-rollback | YES |", md)
        self.assertIn("## Concerns (resolve before executing)\n- Q2 unverified-claims: YES", md)
        self.assertIn("## Riskiest step (model's view)\nStep 1", md)
        self.assertIn("## Evidence by question\n- Q1 unread-files: NO [grounded] `Run the unit tests.`", md)
        self.assertIn("Whether a backup exists.", md)
        self.assertIn("plan check: 3 concern(s), 1 attention, 1 unclear", summary)

    def test_prompt_lists_every_question(self):
        text = dr.render_questions()
        for qid, key, _, _ in dr.PLAN_QUESTIONS:
            self.assertIn(f"Q{qid} {key}:", text)


if __name__ == "__main__":
    unittest.main()
