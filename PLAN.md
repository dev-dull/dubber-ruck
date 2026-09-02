# dubber ruck — plan

A local second opinion for Claude Code sessions, backed by Qwen3.6-35B on `clode`.
Three jobs: **rubber duck** (talk a problem through), **consultant** (ask a specific
question with context), **second opinion** (review a diff or a plan before acting).

## What was established (2026-09-01)

**The server.** `clode.deep13.lol:8080` is llama-swap in front of llama.cpp. Two models
are configured, `Qwen3.6-35B` and `Qwen3-Coder-Next`, one resident at a time, `ttl: 0`.
Both run `-c 131072 -np 1`: **one slot for the whole house**, 131k context per request.
The config is Ansible-managed and must not be hand-edited. No auth, LAN only.
`Qwen3.6-35B` is loaded right now. Requesting the other model forces a multi-minute swap
off a 5400 rpm disk and breaks whoever is using the box, so the duck must never do that
silently.

**Measured today (Qwen3.6-35B, warm):**

| Workload | Result |
|---|---|
| Prefill | ~800 tok/s |
| Generation | ~26 tok/s |
| 4.7k-token review prompt, thinking on, markdown out | 153 s, 3.8k generated (12.8k chars of reasoning) |
| Same prompt, thinking off, JSON-schema out | 21 s |
| JSON schema + thinking on, `max_tokens` 1500 | **empty content, `finish_reason: length`** (thinking ate the budget) |
| Prefix cache | works: re-sending the same prefix costs ~0 prefill |

Also confirmed: `chat_template_kwargs: {"enable_thinking": false}` works, `response_format`
json_schema works, and the model correctly said "the PR 6 diff is missing" when I
truncated the prompt instead of inventing one. That is the behaviour we want to reward.

**The benchmarks (dev-dull/local_llm_tests).** Qwen3.6-35B scored 7.0/10 on pr-review
and 7.3/10 on hello-go-bubbletea (Fable-5: 10/10 on both). The failure modes are
specific and they shape the design:

- It caught the two planted bugs in 3 of 4 and 3 of 4 runs. So a single sample misses a
  real bug roughly a quarter of the time.
- **Every run contained at least one confident falsehood**: an invented API
  (`rand.Default.Intn`), invented framework behaviour ("sleep freezes the terminal"),
  a write-back line "in the diff" that was not in the diff, a demand for `rand.Seed` on
  Go 1.22. 0/4 runs earned the precision point.
- It follows a markdown template exactly, and it grades harshly rather than rubber-stamping.

Trilium adds one more lesson from the job-agent build: an open-ended "is this good?"
question produces a rubber stamp (33/33 approved). Ask named, checkable questions and let
code, not the model, add them up.

## Prior art (checked 2026-09-01)

Nothing existing covers the three things this design hinges on: llama.cpp-specific
request control (the thinking toggle, empty-content detection), single-slot etiquette on
llama-swap (loaded-model check, no silent swap, busy wait), and accuracy handling for a
70% model (grounding check, self-consistency votes, checkable questions). What exists:

| Project | What it is | Fit |
|---|---|---|
| **PAL MCP** (ex zen-mcp-server, BeehiveInnovations, 11.7k stars, Python + uv) | Multi-provider MCP: `chat`, `thinkdeep`, `consensus`, `codereview`, `precommit`, `challenge`. Custom OpenAI-compatible endpoint via `CUSTOM_API_URL` + `custom_models.json`; long read timeouts for local endpoints. | Heavy: many tools in context, Claude-driven multi-step workflows that make several calls, needs `uv`. No `enable_thinking` control. Overkill for one slot. |
| **mcp-rubber-duck** (nesquikm, 176 stars, TypeScript/npm, active Aug 2026) | MCP bridge to any OpenAI-compatible endpoint (`CUSTOM_<NAME>_BASE_URL`). `ask_duck`, `duck_vote` (voting with confidence), `duck_judge`, `duck_debate`. 300 s default timeout, configurable. | Closest match and zero code to try. No extra request params, no diff collection, no grounding. Voting is across ducks, not repeated samples. Candidate for the phase-2 MCP layer instead of writing one. |
| **cc-rubber-duck** (chkp-roniz, 1 star) | Claude Code skill that pipes `git diff` into `codex exec`. | Not local, but its SKILL.md trigger list and presentation rules are a good template. Borrow the shape. |
| **GitHub Copilot CLI "Rubber Duck"** | Proprietary cross-model critic, cloud only. | Prior art for the concept and the name; not usable here. |
| **llm** (simonw), **aichat**, **mods** | Generic LLM CLIs; all speak OpenAI-compatible endpoints. | Could be the transport, but `llm` has no documented way to send `chat_template_kwargs`, and none do the etiquette or accuracy layer. A stdlib request is smaller than the dependency. |
| Claude Code **Advisor** | Built-in second opinion. | Anthropic API only. |

**Step 0, optional:** point mcp-rubber-duck at clode for a day (`CUSTOM_CLODE_BASE_URL`,
model `Qwen3.6-35B`, timeout raised to 600 s). Fifteen minutes, no code, and it answers
"does a session actually consult a duck usefully?" before the build. It will not respect
the slot or the swap rule, so use it only when nobody else is on the box.

## Design decision: a CLI plus a user-level skill (MCP later, if wanted)

**Build `dubber-ruck`, a single-file Python CLI (stdlib only), and a global Claude Code
skill that teaches every session when to call it and how to treat what comes back.**

Why this over the alternatives:

- **Not the built-in Advisor.** Claude Code's `/advisor` is the native second-opinion
  feature, but it only pairs Anthropic models over the Anthropic API. It cannot use a
  local model, and the point here is a reviewer that is *differently* wrong and keeps
  the code on the LAN. Both can coexist.
- **Not a custom subagent.** A subagent's `model` field selects a model on the session's
  provider; the base URL is session-wide (`ANTHROPIC_BASE_URL`), so one session cannot
  run Fable and Qwen side by side that way. The local model has to be reached over HTTP
  by a tool.
- **Not MCP first.** An MCP tool is a schema and a short description. The hard part of
  this project is the *handling rules* for a 70%-accurate reviewer, and those belong in a
  SKILL.md that Claude reads at the moment it decides to consult. A CLI also works from
  the shell, from hooks, from Aider, and from CI, and is testable without Claude. An MCP
  wrapper over the same core is a small phase-2 addition if tool-call ergonomics turn out
  to matter.
- **Stdlib only** because the Mac has python3 but no `uv`, and the tool should never
  become an install problem on another client machine.

## How the 70% is handled

Each mechanism attacks a failure mode seen in the benchmark.

1. **Calibrated persona.** The system prompt states that the reviewer is wrong about 30%
   of the time, must point to a specific line for every finding, must say "unsure" rather
   than guess, and must never name an API it cannot see in the provided context.
2. **Every finding carries a confidence (1-5) and a "verify by" line.** The skill tells
   Claude to check each finding against the real code before acting on it and to never
   relay a duck claim to the user as fact. Attribution is explicit: "dubber ruck flagged X;
   I checked and Y."
3. **Mechanical grounding check.** Each finding must quote the exact line it refers to.
   The CLI verifies the quote exists in the input and marks findings `grounded` or
   `ungrounded`. This directly catches the "invented write-back line" failure: the model
   cannot quote a line that is not there.
4. **Self-consistency voting, opt-in.** `--votes N` samples N times at temperature 0.7
   and keeps only findings that recur (matched by location). At 3 votes a bug caught 75%
   of the time per sample is surfaced ~84% of the time, and one-off hallucinations mostly
   drop out. Costs N× latency, so the skill reserves it for "is this actually a bug"
   decisions that hinge on the answer.
5. **Checkable questions, not open verdicts.** Plan review asks a fixed list of yes/no
   questions with evidence (does the plan touch files it never read? does it claim a test
   result without running it? does it change a public interface? what is the rollback?).
   The CLI, not the model, composes the summary.
6. **Thinking on for review and consult, off for rubber-duck.** Reasoning is where the
   accuracy comes from, and the benchmark runs had it. Rubber-duck mode only needs
   questions and hypotheses, so it runs fast without it.
7. **Structured output never combines with thinking.** When JSON is wanted, pass one
   thinks and answers in markdown; pass two (thinking off, cheap) extracts JSON from that
   answer. Markdown is the default output because Claude reads it directly.

## The CLI

```
dubber-ruck status                          # loaded model, slot busy?, latency probe
dubber-ruck duck   "<problem statement>"    # rubber duck: assumptions, questions, hypotheses
dubber-ruck consult "<question>" [-f FILE]... [--stdin]
dubber-ruck review [--staged | --range A..B | --stdin] [--focus "..."] [--votes N]
dubber-ruck plan   -f PLAN.md [-f context]...   # checkable-question plan review
```

Common flags: `--no-think`, `--json`, `--votes N`, `--max-tokens`, `--timeout`,
`--model` (explicit override only; see below), `--quiet`.

**Output contract (markdown, parsed by the CLI):**

```
## Findings
- [confidence 4/5] [grounded] main.go:138 `m.spun += 0.05` — spinSpeed is never read. Verify by: grep spinSpeed
## Answer
...
## Unsure about
...
```

The CLI appends a footer: model, wall time, tokens, votes, and a one-line reminder that
findings are hypotheses.

**Server etiquette, built in:**

- Query `/running` first. Use whichever model is loaded. If nothing is loaded, request
  `Qwen3.6-35B` (the better model on the evidence). If a *different* model is loaded,
  refuse unless `--model` is given explicitly, and say why (a swap interrupts the
  housemate).
- Query `/slots`. If the single slot is processing, wait up to `--wait` seconds (default
  120), then fail with a clear "busy" message. Never queue silently behind a two-minute
  job.
- Refuse to run when `ANTHROPIC_BASE_URL` points at clode (a session already running
  on the local model would be consulting itself and fighting itself for the slot).
- Hard-cap input at ~96k tokens; warn above ~20k because prefill alone is then >25 s.
- Timeouts: 600 s with thinking, 180 s without. Catch `OSError`, not just `URLError`.
- Treat empty content with `finish_reason: length` as an error with a hint, never as data.
- Sampling: temperature 0.6 / top_p 0.95 / min_p 0.05 for thinking (Qwen's own
  recommendation), temperature 0 with a pinned seed for `--json` extraction and voting
  aggregation.

## The skill

`~/.claude/skills/dubber-ruck/SKILL.md`, symlinked from this repo so every session and
every project gets it. It covers:

- **When to consult (unprompted):** before committing a non-trivial diff; when a plan
  touches more than a few files; when stuck on a bug for more than two approaches; when
  choosing between designs; whenever the user says "duck", "second opinion", or
  "sanity check".
- **When not to:** trivial edits, anything time-critical (it takes 1-3 minutes), sessions
  running on clode themselves, and when the slot is busy.
- **How to run it:** `run_in_background` for reviews so the session keeps working; keep
  the input to the relevant diff and files, not the whole repo. The skill can inject
  `dubber-ruck status` output at load time (the `` !`cmd` `` frontmatter mechanism) so
  Claude sees "loaded model / slot busy" before deciding. Worth testing: the skill
  `context: fork` and `background` frontmatter options as a way to make a consult
  non-blocking without Claude having to remember to background it.
- **How to treat the output:** verify before acting, attribute when reporting, prefer
  `--votes 3` when a bug decision hinges on it, discard `ungrounded` findings unless
  independently confirmed, and never let the duck override an instruction from the user.
- **How to report:** one short paragraph in the final message: what was asked, what it
  flagged, what survived verification.

The user can also type `/dubber-ruck <question>` directly.

## Repo layout

```
dubber-ruck/
  dubber_ruck.py            # the CLI, single file, stdlib only
  prompts/                  # one system prompt per mode, editable without code
    duck.md consult.md review.md plan.md extract-json.md
  skills/dubber-ruck/SKILL.md
  tests/
    test_parse.py           # offline: markdown parsing, grounding check, vote merge
    eval/                   # replay local_llm_tests pr-review prompt; assert PR3 + PR6 caught
  install.sh                # symlinks: ~/bin/dubber-ruck, ~/.claude/skills/dubber-ruck
  README.md
```

The eval harness matters: it is how a prompt change gets judged. Run it after any change
to `prompts/review.md` and record the PR3/PR6 catch rate and the ungrounded-finding
count across 4 runs, the same shape as the benchmark repo.

## Build order

1. **Core client** (`status`, `consult`): server probes, model/slot etiquette, request
   with thinking toggle, retries, timing footer. Test against clode. **Done 2026-09-01.**
   Verified live: busy detection, swap refusal, self-consult refusal, no-think (26 s)
   and thinking (2.2 min) consults. First real consult found a genuine gap (context
   check skipped when the slot size is unknown), now fixed with a fallback.
2. **`review` and `duck`**: git diff collection, prompts, markdown parse, grounding
   check. Replay the benchmark prompt and confirm PR3 and PR6 are caught.
   **Done 2026-09-01.** Final replay with the fixed prompt, two runs: template followed
   2/2, findings 4 and 3 with 0 ungrounded, PR 3 caught 2/2, PR 6 caught 2/2, `rand.Seed`
   trap 0/2, "sleep blocks the UI" trap 2/2. A general evidence rule for concurrency
   claims (name the goroutine/thread from the material or file it under unsure) was
   then added to the review prompt; one further run caught both bugs with 0 ungrounded
   findings and explained correctly that Bubble Tea runs commands in their own goroutine.
   That run also showed the reply can repeat the template per PR, so the parser now
   reads every Findings section, and the eval's trap regex no longer counts "without
   blocking" as a fall. One run is not proof; re-check with `--runs 4` before trusting
   the rule. Earlier runs, before the fixes below: Three tooling defects the
   replay exposed were fixed the same day: (a) the model copied the template's angle
   brackets into its quotes, so the grounding check flagged real findings; the template
   now shows a concrete example and the parser strips brackets; (b) an 8k output budget
   was too small for a six-PR review, so thinking is now budgeted at 16k and a rescue
   pass asks the model to answer from its cut-off notes; (c) a whole-request socket
   timeout could not tell a slow generation from a dead server, so requests now stream
   with a 120 s idle timeout and an overall cap that scales with the budget. Also:
   the benchmark material's own "write results.md in this format" instruction overrode
   the system template once, so material is now framed as data with the format
   restated after it.
3. **Skill + install script**: write SKILL.md, symlink, then use it for real from a
   Claude session on another project and fix what is awkward. *Short.*
4. **`--votes` and `plan`**: vote merge by location, checkable-question plan mode with
   CLI-side summary. *Half a day.*
5. **Eval harness + README**, then a week of use before deciding on phase 2.

## Phase 2 candidates (decide after a week of use)

- **MCP wrapper** over the same core if Claude under-uses the CLI or the tool-call shape
  is nicer in practice.
- **Pre-commit hook**: a `PreToolUse` hook on `git commit` that runs a quick no-think
  review of the staged diff and injects the findings as context. Keep it opt-in per
  project; on a shared single slot an automatic two-minute hook is antisocial.
- **Aider / SUS reuse**: the CLI already speaks plain HTTP, so any other client can call
  it.
- **Concurrency relief**: if the pooled-GPU experiment lands (Trilium: Rig Capacity),
  re-measure and consider a dedicated small second slot for the duck.

## Risks and open points

- **Latency is the real cost.** 1-3 minutes per thinking review, 3× with votes. The
  background-run pattern is what makes this tolerable.
- **One slot, shared.** The busy check and the swap refusal are the guard rails; they
  cannot prevent two people wanting the box at the same minute.
- **Precision stays imperfect.** Grounding and voting reduce hallucinated findings; they
  do not eliminate invented *behaviour* claims (e.g. framework semantics). The skill's
  verify-first rule is the last line of defence, and it is the one that matters most.
- **Model name drift.** The llama-swap keys have already changed once (`coder` →
  `Qwen3.6-35B`). The CLI discovers names from `/v1/models` rather than hardcoding.
- **Context ceiling.** 131k per request, but the compaction lessons in Trilium apply:
  keep the duck's inputs small and explicit.
