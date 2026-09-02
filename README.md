# dubber ruck

A local second opinion for Claude Code sessions, backed by Qwen3.6-35B on `clode`
(llama-swap + llama.cpp). Standard-library Python, no dependencies.

The model is right about 70% of the time on code-review benchmarks, so the tool is
built around that: every finding carries a confidence and a "verify by" line, and the
footer reminds the reader that findings are hypotheses. Design and reasoning: `PLAN.md`.

## Install

```
./install.sh          # symlinks ~/bin/dubber-ruck and ~/.claude/skills/dubber-ruck
dubber-ruck status    # what is loaded, is the slot busy
```

The skill (`skills/dubber-ruck/SKILL.md`) is what makes every Claude Code session use
the tool well: when to consult unprompted, how to run thinking calls in the background,
and the rules for treating a 70%-accurate reviewer's output (verify before acting,
discard ungrounded findings, attribute by name, the user's instructions win). It
injects `dubber-ruck status` at load time so Claude sees the slot state before
deciding. Type `/dubber-ruck` in a session to invoke it directly.

Configuration is by environment variable:

| Variable | Default | Meaning |
|---|---|---|
| `DUBBER_RUCK_URL` | `http://clode.deep13.lol:8080` | llama-swap base URL |
| `DUBBER_RUCK_MODEL` | `Qwen3.6-35B` | preferred model when nothing is loaded |
| `DUBBER_RUCK_CTX` | `131072` | per-slot context if the server cannot report it |
| `DUBBER_RUCK_PROMPTS` | `./prompts` | directory of system prompts |

## Use

```
dubber-ruck status  [--probe] [--json]
dubber-ruck consult "question" [-f FILE ...] [--stdin] [--votes N]
dubber-ruck review  [--staged | --range A..B | --commit REV | --stdin] [--focus TEXT] [--with-files] [--votes N]
dubber-ruck duck    "what I am stuck on" [-f FILE ...]
dubber-ruck plan    PLAN.md [-f CONTEXT ...]
```

- **consult** asks a specific question, with files as context. Thinking on by default.
- **review** sends a diff (working tree vs HEAD by default, plus untracked files) and
  asks for defects and clearly better alternatives, with a SHIP / FIX FIRST / RETHINK
  verdict. Thinking on by default.
- **duck** is the rubber duck: it names the assumptions in your problem statement,
  asks questions, ranks hypotheses, and suggests the cheapest next check. It does not
  solve anything. Thinking off by default, so it answers in about 20-30 seconds.

- **plan** asks eight fixed, checkable questions about a plan (files it never read,
  claims it never verifies, assumptions stated as fact, irreversible steps without a
  rollback, interface changes, scope, a simpler alternative, the riskiest step). Each
  answer must quote the plan, and the CLI, not the model, turns the answers into a
  verdict: NOT READY, READY WITH NOTES, or READY. This sidesteps the rubber-stamp
  problem: an open "is this plan good?" gets approved every time.

**`--votes N`** (consult and review) samples the model N times with different seeds
and keeps only findings a majority of runs agree on; the rest are listed under
"Dropped". The verdict is the majority verdict, ties going to the cautious side. It
costs N times the wall time, so use it when a decision hinges on whether something is
really a bug. With per-sample recall around 75%, three votes surface a real bug about
84% of the time and one-off hallucinations mostly fall out.

Thinking mode is slower and more accurate: expect 1-3 minutes for a few thousand
tokens of input. `--no-think` and `--think` override the per-mode default.
`--dry-run` prints the input size and a time estimate without sending anything.

### Reading the output

Consult and review findings look like this:

```
- [confidence 4/5] [grounded] main.go:138 `m.spun += 0.05` — spinSpeed is never read. Verify by: grep spinSpeed
```

The first tag is the model's own confidence. The second is added by the tool: it
checks that the longest quoted span in the finding actually occurs in the material
sent. `[grounded]` means it does. `[UNGROUNDED ...]` means the model quoted a line
that is not there, which is its most characteristic hallucination, so treat that
finding as suspect. `[unquoted]` means there was nothing to check. The footer gives
the totals.

### Replaying the benchmark

```
python3 tests/eval/pr_review_eval.py --runs 4
```

Sends the `local_llm_tests` pr-review prompt through `review` and reports, per run,
whether the two planted bugs were caught, whether the two known traps were fallen
into, and how many findings were ungrounded. Run it after changing `prompts/review.md`.
Each run occupies the slot for 2-3 minutes.

## Guard rails

- Uses whichever model is already loaded. Refuses to trigger a model swap unless you
  pass `--model X --allow-swap`, because a swap takes minutes and interrupts anyone
  else using the box.
- Waits for the single slot if it is busy (`--wait`, default 120 s), then gives up
  with exit code 3 rather than queueing silently.
- Refuses to run when `ANTHROPIC_BASE_URL` already points at the same host
  (`--force` overrides).
- Rejects input that would not fit the slot, and warns above ~20k tokens.
- Streams the response, so a server that stops sending is noticed within the idle
  timeout (120 s) while a slow generation can run to an overall cap that scales with
  the output budget.
- If the reasoning phase uses the whole budget, a rescue pass hands the model its own
  notes and asks for the answer with thinking off (`--no-rescue` to fail instead).
- Frames attached material as data, with the required output format restated after
  it, so instructions inside a diff or document do not redirect the review.

Exit codes: 0 ok, 1 error, 2 unreachable, 3 busy, 4 refused, 5 empty output, 6 too large.

## Tests

```
python3 -m unittest discover -s tests
```
