# dubber ruck: design notes

Why the tool is shaped the way it is. The README says what it does; this says why.

## The premise

A small model you host yourself is a useful reviewer for a very specific reason: it
has no memory of how the work got here. A reviewer without that memory catches
assumption drift the author cannot see, whether or not it is smarter. It is also
wrong a meaningful fraction of the time.

The design was calibrated against a concrete measurement: on a six-PR code-review
benchmark with two planted bugs and two known traps
([local_llm_tests](https://github.com/dev-dull/local_llm_tests)), a 35B-parameter
mixture-of-experts model at 4-bit quantisation caught each planted bug in three of
four runs and produced at least one confident falsehood in every run: an invented
API, invented framework behaviour, a "quoted" line that was not in the diff, a
requirement the language version had made obsolete. Roughly 7 out of 10 on that
rubric. Your model will differ; the mechanisms below are what make a reviewer in that
accuracy range safe to use, and the prompts in `prompts/` state the calibration
explicitly so you can adjust it.

## How an imperfect reviewer is handled

Each mechanism attacks a failure mode that was actually observed.

1. **Calibrated persona.** The system prompt tells the model it is wrong about a third
   of the time, must point to a specific line for every finding, must say "unsure"
   rather than guess, and must not name an API it cannot see in the material.
2. **Confidence and a "verify by" line on every finding.** The reader is expected to
   check each finding before acting. The skill tells the assistant exactly that, and to
   attribute the duck by name rather than presenting its claims as its own.
3. **Mechanical grounding.** Each finding must quote the line it is about. The tool
   checks that the quote occurs in the material that was sent, tolerant of whitespace,
   dropped backticks, and stitched quotes, and tags the finding `[grounded]` or
   `[UNGROUNDED]`. This catches the model's most characteristic hallucination: a line
   in the diff that is not in the diff. It cannot catch invented *behaviour*, which is
   why rule 2 is the load-bearing one.
4. **Self-consistency voting**, opt-in (`--votes N`): sample N times with different
   seeds and keep findings a majority agree on. With per-sample recall around 75%,
   three votes surface a real bug about 84% of the time and one-off hallucinations
   mostly fall out. It costs N× wall time.
5. **Checkable questions instead of open verdicts** for plans. An open "is this plan
   good?" is rubber-stamped every time. The plan mode asks eight fixed yes/no questions
   with quoted evidence, and the tool, not the model, turns the answers into a verdict.
6. **Thinking on for review and consult, off for the rubber duck.** The reasoning
   phase is where the accuracy comes from. Structured output never combines with
   thinking, because the reasoning eats the token budget and the answer comes back
   empty; when that happens anyway, a rescue pass hands the model its own notes and
   asks for the answer with thinking off.
7. **Material is framed as data.** A diff or document can contain instructions of its
   own; the prompt says they are part of what is being examined, and the required
   output format is restated after the material.

## Why a CLI and a skill, not an MCP server or a subagent

- A Claude Code subagent cannot point at a different API base URL; the local model has
  to be reached over HTTP by a tool.
- The built-in Advisor pairs Anthropic models over the Anthropic API only.
- An MCP tool is a schema and a short description. The hard part of this project is the
  handling rules for an imperfect reviewer, and those belong in a skill the assistant
  reads at the moment it decides to consult. A CLI also works from the shell, from
  hooks, and from other clients, and is testable without an assistant in the loop.
- Standard library only, so it never becomes an install problem on another machine.

Existing projects in the same space (PAL/zen-mcp-server, mcp-rubber-duck, GitHub
Copilot's Rubber Duck, generic LLM CLIs) cover multi-provider plumbing and debates,
none of them the three things this design hinges on: control of llama.cpp's thinking
toggle, etiquette on a shared single-slot server, and the grounding and voting layer.

## Why hooks

After a day of real use with the skill alone, the duck was consulted only when asked
for by name, never at the checkpoints the skill describes, even on a fresh project that
began with a planning round. Auto-invocation from a skill listing is a judgment the
assistant makes, and it is conservative. The two checkpoints that can be detected
mechanically, a `git commit` and leaving plan mode, are therefore enforced by
PreToolUse hooks that inject the review as context. The other two, a fix that has
failed twice and a destructive step, still rely on the assistant, helped by a section in
the user-level `CLAUDE.md` that sits in every session's context.

Things the hooks had to learn the hard way: PreToolUse runs before the command, so
`git add && git commit` has nothing staged yet (the working tree is reviewed instead);
the payload carries the session's directory, not the `cd` target inside the command;
`git -c k=v commit` is still a commit; and "git -C DIR" inside a commit message is
prose, not an option.

## Etiquette for a shared server

With llama-swap in front of llama.cpp there is typically one model resident and one
request slot. The tool therefore uses whatever model is loaded, refuses to trigger a
swap unless told to explicitly (a cold load takes minutes and interrupts whoever else
is using the box), waits on a busy slot and then gives up rather than queueing
silently, and refuses to run when the session itself is pointed at the same server.
Against a plain OpenAI-compatible endpoint none of that state is visible, and the tool
degrades to sending chat completions with the configured model.

## Evaluating prompt changes

`tests/eval/pr_review_eval.py` replays the benchmark prompt and records, per run,
whether the planted bugs were caught, whether the traps were fallen into, and how many
findings were ungrounded. Results are written to `tests/eval/results/` keyed by the
review prompt's hash, so the effect of a prompt change is in git. Two lessons from
using it: one run proves nothing (a rule that "fixed" a trap in one run failed in three
of the next four), and the scorer needs the same scepticism as the model (it once
counted "without blocking the UI", the correct answer, as a fall).
