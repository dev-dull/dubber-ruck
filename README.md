# dubber ruck

A second opinion for Claude Code sessions from a model you host yourself. Point it at
any OpenAI-compatible chat endpoint; with [llama-swap](https://github.com/mostlygeek/llama-swap)
in front of llama.cpp it also handles model state and shared-slot etiquette. Nothing
leaves your network. Standard-library Python 3.10+, no dependencies.

Small local models are right most of the time but not always. The tool is built around
that: every finding carries a confidence and a "verify by" line, the tool checks that
each quoted line really exists in what was sent, and votes and fixed checkable
questions are available for decisions that matter. The reasoning is in
[docs/DESIGN.md](docs/DESIGN.md).

## How it works

```
Claude Code session ──(skill / hooks)──> dubber-ruck CLI ──HTTP──> your server (llama-swap or any OpenAI-compatible endpoint)
                                            │
                                            ├─ etiquette (llama-swap): loaded model only, no silent swap, wait on the one slot
                                            ├─ request: streaming, thinking on/off, rescue when reasoning runs long
                                            └─ output: parse findings, ground quotes, vote, or score a plan
```

## Install

```
./install.sh                              # symlinks ~/bin/dubber-ruck and ~/.claude/skills/dubber-ruck,
                                          # creates ~/.config/dubber-ruck/config from config.example
$EDITOR ~/.config/dubber-ruck/config      # set DUBBER_RUCK_URL, and DUBBER_RUCK_MODEL unless the server offers one model
dubber-ruck status --probe                # server type, what is loaded, round-trip time
```

| Setting | Default | Meaning |
|---|---|---|
| `DUBBER_RUCK_URL` | `http://localhost:8080` | server base URL |
| `DUBBER_RUCK_MODEL` | unset | on llama-swap: the preferred model (whatever is loaded is used, with a warning); on a plain server: the model to ask for, required unless the server offers exactly one |
| `DUBBER_RUCK_CTX` | `131072` | context window per request, if the server cannot report it |
| `DUBBER_RUCK_PREFILL_TPS`, `DUBBER_RUCK_GEN_TPS` | 800, 26 | your hardware's throughput, used only for the time estimate |
| `DUBBER_RUCK_COMMIT_HOOK`, `DUBBER_RUCK_PLAN_HOOK` | quick, think | hook modes: `off`, `quick`, `think` |
| `DUBBER_RUCK_PROMPTS` | `./prompts` | directory of system prompts |

Environment variables override the config file. The prompts in `prompts/` state that
the model is wrong about a third of the time; if yours is much better or worse, edit
that calibration.

The skill (`skills/dubber-ruck/SKILL.md`) is what makes a session use the tool well:
when to consult unprompted, how to run thinking calls in the background, and the rules
for treating an imperfect reviewer's output (verify before acting, discard ungrounded
findings, attribute by name, the user's instructions win). It injects `dubber-ruck
status` at load time so the assistant sees the server state before deciding. Type
`/dubber-ruck` in a session to invoke it directly.

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
  solve anything. Thinking off by default, so it answers quickly.
- **plan** asks eight fixed, checkable questions about a plan (files it never read,
  claims it never verifies, assumptions stated as fact, irreversible steps without a
  rollback, interface changes, scope, a simpler alternative, the riskiest step). Each
  answer must quote the plan, and the CLI, not the model, turns the answers into a
  verdict: NOT READY, READY WITH NOTES, or READY. An open "is this plan good?" gets
  approved every time; fixed questions do not.

**`--votes N`** (consult and review) samples the model N times with different seeds and
keeps only findings a majority of runs agree on; the rest are listed under "Dropped".
The verdict is the majority verdict, ties going to the cautious side. It costs N times
the wall time, so use it when a decision hinges on whether something is really a bug.

Thinking mode is slower and more accurate; how much slower depends on your hardware
(minutes on a CPU-offloaded mixture-of-experts model, seconds on a GPU-resident dense
one). `--no-think` and `--think` override the per-mode default. `--dry-run` prints the
input size and a time estimate without sending anything. `--dump-raw PATH` saves the
model's unprocessed answer, and `--dump-reasoning PATH` its hidden reasoning.

### Reading the output

Consult and review findings look like this:

```
- [confidence 4/5] [grounded] main.go:138 `m.spun += 0.05` — spinSpeed is never read. Verify by: grep spinSpeed
```

The first tag is the model's own confidence. The second is added by the tool: it checks
that the quoted line actually occurs in the material sent. `[grounded]` means it does.
`[UNGROUNDED ...]` means the model quoted a line that is not there, which is a small
model's most characteristic hallucination, so treat that finding as suspect.
`[unquoted]` means there was nothing to check. The footer gives the totals.

## Hooks: the checkpoints the harness enforces

Left to its own judgment, a Claude Code session consults the duck only when asked, so
the two checkpoints that can be detected mechanically can be enforced by hooks
(`hooks/hook.py`; `install.sh` prints the settings block to merge into
`~/.claude/settings.json`):

| event | what runs | default mode |
|---|---|---|
| `git commit` (PreToolUse on Bash) | `review` of the staged diff, or of the working tree when the command stages first or uses `-a` | quick (thinking off) |
| leaving plan mode (PreToolUse on ExitPlanMode) | `plan` on the newest file in `~/.claude/plans` | think |

The result is injected as context; the tool call itself is never blocked. A hook skips
silently, and says why in `~/.claude/dubber-ruck-hook.log`, when the diff is trivial
(under 8 changed lines), the same content was reviewed within the hour, the slot is
busy, the server is unreachable, the session itself runs on the local server, or, on
llama-swap, the loaded model is not the preferred one.

The other two checkpoints (a fix that has failed twice, a destructive step) and plans
drafted outside plan mode cannot be detected by a hook. For those, the skill's
description and a short section in your user-level `~/.claude/CLAUDE.md` (in every
session's context) tell the assistant to consult proactively. A suggested section is
in `docs/CLAUDE.md.example`.

## Guard rails

- On llama-swap: uses whichever model is already loaded and refuses to trigger a swap
  unless you pass `--model X --allow-swap`, because a swap takes minutes and
  interrupts anyone else using the server; waits for a busy slot (`--wait`, default
  120 s) and then gives up with exit code 3 rather than queueing silently.
- Refuses to run when `ANTHROPIC_BASE_URL` already points at the same host
  (`--force` overrides): a session running on the local model would be consulting
  itself and competing for its own slot.
- Rejects input that would not fit the context window, and warns above ~20k tokens.
- Streams the response, so a server that stops sending is noticed within the idle
  timeout (120 s) while a slow generation can run to an overall cap that scales with
  the output budget.
- If the reasoning phase uses the whole budget, a rescue pass hands the model its own
  notes and asks for the answer with thinking off (`--no-rescue` to fail instead).
- Frames attached material as data, with the required output format restated after
  it, so instructions inside a diff or document do not redirect the review.

Exit codes: 0 ok, 1 error, 2 unreachable, 3 busy, 4 refused, 5 empty output, 6 too large.

## Replaying the benchmark

```
python3 tests/eval/pr_review_eval.py --runs 4 --label "what changed"
```

Sends the pr-review prompt from [local_llm_tests](https://github.com/dev-dull/local_llm_tests)
through `review` and reports, per run, whether the two planted bugs were caught,
whether the two known traps were fallen into, and how many findings were ungrounded.
Run it after changing `prompts/review.md`. Results are written to
`tests/eval/results/<date>-<prompt hash>.md`, so the history of prompt changes and
their effect is in git.

## Layout

```
dubber_ruck.py               the CLI, one file
prompts/                     system prompts per mode: consult, review, duck, plan
skills/dubber-ruck/SKILL.md  the Claude Code skill (symlinked into ~/.claude/skills)
hooks/hook.py                the commit and plan-mode hooks
config.example               settings template for ~/.config/dubber-ruck/config
docs/                        design notes and the CLAUDE.md section
tests/                       offline unit tests, incl. an end-to-end fake llama-swap
tests/eval/                  the benchmark replay and its recorded results
install.sh                   symlinks and first-run config
```

## Tests

```
python3 -m unittest discover -s tests
```
