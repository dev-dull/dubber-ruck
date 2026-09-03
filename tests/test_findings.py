"""Offline tests for the findings parser, grounding check, and annotation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ["DUBBER_RUCK_CONFIG"] = "/nonexistent/dubber-ruck-config"
for _k in [k for k in os.environ if k.startswith("DUBBER_RUCK_") and k != "DUBBER_RUCK_CONFIG"]:
    del os.environ[_k]
import dubber_ruck as dr  # noqa: E402

MATERIAL = """\
diff --git a/main.go b/main.go
+++ b/main.go
@@ -130,7 +130,7 @@ func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
 	case tickerMsg:
-		m.spun += 0.05
+		m.spun += 0.05
+		for i, c := range m.rainPos {
+			c.y += c.v
+		}
"""

OUTPUT = """\
## Findings
- [confidence 5/5] main.go Update `m.spun += 0.05` — spinSpeed is never read. Verify by: grep spinSpeed
- [4/5] main.go tick loop `for i, c := range m.rainPos {` — c is a copy; mutations are lost.
  Verify by: reading the loop body.
- [3/5] main.go `m.rainPos[i] = c` — write-back is missing a bounds check. Verify by: run it
- [2/5] general — the sleep blocks the terminal. Verify by: trust me

## Answer
**Verdict:** FIX FIRST
Two real bugs.

## Unsure about
Whether Go 1.22 is in use.
"""


class Parse(unittest.TestCase):
    def test_sections(self):
        s = dr.split_sections(OUTPUT)
        self.assertIn("findings", s)
        self.assertTrue(s["answer"].startswith("**Verdict:** FIX FIRST"))
        self.assertEqual(s["unsure about"], "Whether Go 1.22 is in use.")

    def test_parses_both_tag_forms_and_continuations(self):
        f = dr.parse_findings(OUTPUT)
        self.assertEqual([x.confidence for x in f], [5, 4, 3, 2])
        self.assertIn("reading the loop body", f[1].text)
        self.assertEqual(f[0].quotes, ["m.spun += 0.05"])

    def test_angle_bracket_placeholders_are_stripped(self):
        f = dr.parse_findings("## Findings\n- [5/5] PR 3 `<m.spun += 0.05>` — copied the template literally")
        self.assertEqual(f[0].quotes, ["m.spun += 0.05"])
        dr.ground(f, MATERIAL)
        self.assertTrue(f[0].grounded)

    def test_repeated_template_blocks_are_all_parsed_and_annotated(self):
        md = ("## Findings\n- none\n\n## Answer\nok\n\n## Unsure about\nno\n\n"
              "## Findings\n- [5/5] PR 3 `m.spun += 0.05` — dead field\n\n## Answer\nfix\n\n"
              "## Findings\n- [4/5] PR 6 `for i, c := range m.rainPos {` — copy\n- [1/5] PR 6 `m.rainPos[i] = c` — invented\n\n## Answer\nfix\n")
        f = dr.parse_findings(md)
        self.assertEqual([x.confidence for x in f], [5, 4, 1])
        dr.ground(f, MATERIAL)
        self.assertEqual([x.grounded for x in f], [True, True, False])
        out = dr.annotate(md, f)
        self.assertEqual(out.count("[grounded]"), 2)
        self.assertEqual(out.count("[UNGROUNDED"), 1)
        self.assertEqual(out.count("## Answer"), 3)
        self.assertIn("- [confidence 5/5] [grounded] PR 3", out)
        self.assertTrue(out.endswith("fix\n"))

    def test_none_marker_yields_no_findings(self):
        self.assertEqual(dr.parse_findings("## Findings\n- none\n\n## Answer\nfine"), [])

    def test_missing_section(self):
        self.assertEqual(dr.parse_findings("just prose"), [])


class Ground(unittest.TestCase):
    def test_grounding_flags_invented_line(self):
        f = dr.parse_findings(OUTPUT)
        dr.ground(f, MATERIAL)
        self.assertEqual([x.grounded for x in f], [True, True, False, None])

    def test_whitespace_and_diff_prefix_ignored(self):
        f = dr.parse_findings("## Findings\n- [5/5] x `for i, c := range   m.rainPos {` — y")
        dr.ground(f, MATERIAL)
        self.assertTrue(f[0].grounded)

    def test_longest_span_decides(self):
        # A real identifier must not vouch for an invented line.
        f = dr.parse_findings("## Findings\n- [5/5] `rainPos` `m.rainPos = append(m.rainPos, c)` — invented")
        dr.ground(f, MATERIAL)
        self.assertFalse(f[0].grounded)

    def test_loose_match_ignores_formatting_characters(self):
        material = "The CLI verifies the quote exists and marks findings `grounded` or `ungrounded`.\n**Build order**\n\n1. **Core client** (`status`, `consult`)"
        f = dr.parse_findings("## Findings\n- [4/5] x `marks findings grounded or ungrounded` — copied without the backticks")
        dr.ground(f, material)
        self.assertTrue(f[0].grounded)

    def test_stitched_quote_needs_every_segment(self):
        material = "alpha beta gamma delta epsilon\nzeta eta theta iota kappa lambda\n"
        good = dr.parse_findings("## Findings\n- [4/5] x `alpha beta gamma delta ... eta theta iota kappa` — two real pieces")
        bad = dr.parse_findings("## Findings\n- [4/5] x `alpha beta gamma delta / invented words here now` — one invented piece")
        dr.ground(good, material)
        dr.ground(bad, material)
        self.assertTrue(good[0].grounded)
        self.assertFalse(bad[0].grounded)

    def test_explanation_spans_do_not_override_the_quoted_line(self):
        md = ("## Findings\n- [5/5] main.go `m.spun += 0.05` — compare with the old form "
              "`for i, c := range m.rainPos { c.y += c.v; c.col = pick(c.col, i) }` which is not in the diff.\n"
              "Wait, let me look closer at `func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) { switch msg := msg.(type) {`\n")
        f = dr.parse_findings(md)
        self.assertEqual(f[0].quotes, ["m.spun += 0.05"])
        dr.ground(f, MATERIAL)
        self.assertTrue(f[0].grounded)

    def test_unspaced_em_dash_still_splits_but_hyphen_in_code_does_not(self):
        f = dr.parse_findings("## Findings\n- [5/5] main.go `m.spun += 0.05`—see `for i, c := range m.rainPos {` too")
        self.assertEqual(f[0].quotes, ["m.spun += 0.05"])
        g = dr.parse_findings("## Findings\n- [5/5] main.go `m.rainPos[i-1]` — off by one")
        self.assertEqual(g[0].quotes, ["m.rainPos[i-1]"])

    def test_falls_back_to_explanation_spans_when_head_has_none(self):
        f = dr.parse_findings("## Findings\n- [4/5] somewhere — the loop `for i, c := range m.rainPos {` copies c")
        self.assertEqual(f[0].quotes, ["for i, c := range m.rainPos {"])

    def test_attachment_names_do_not_ground(self):
        f = dr.parse_findings("## Findings\n- [5/5] `main.go` — vague")
        dr.ground(f, "### File: main.go\n" + MATERIAL, ignore={"main.go"})
        self.assertIsNone(f[0].grounded)


class Annotate(unittest.TestCase):
    def test_tags_inserted_and_rest_preserved(self):
        f = dr.parse_findings(OUTPUT)
        dr.ground(f, MATERIAL)
        out = dr.annotate(OUTPUT, f)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("- [confidence 5/5] [grounded] main.go"))
        self.assertTrue(lines[2].startswith("- [confidence 4/5] [grounded] main.go"))
        self.assertIn("[UNGROUNDED", lines[4])
        self.assertIn("[unquoted]", lines[5])
        self.assertIn("## Answer\n**Verdict:** FIX FIRST", out)
        self.assertTrue(out.rstrip().endswith("Whether Go 1.22 is in use."))

    def test_summary(self):
        f = dr.parse_findings(OUTPUT)
        dr.ground(f, MATERIAL)
        self.assertEqual(dr.grounding_summary(f), "findings: 4 (2 grounded, 1 UNGROUNDED (treat as suspect), 1 unquoted)")
        self.assertIsNone(dr.grounding_summary([]))


class Git(unittest.TestCase):
    def test_touched_paths(self):
        self.assertEqual(dr.touched_paths(MATERIAL + "+++ b/other.go\n+++ b/main.go\n"), ["main.go", "other.go"])


if __name__ == "__main__":
    unittest.main()
