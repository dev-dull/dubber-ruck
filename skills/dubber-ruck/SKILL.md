---
name: dubber-ruck
description: Use this skill PROACTIVELY, without being asked, at these checkpoints - (1) after drafting any plan or design that touches 3+ files or makes a design choice, before presenting it; (2) before committing a diff that is more than a few lines; (3) when the same bug has survived two attempted fixes; (4) before any destructive or hard-to-reverse step. Also whenever the user says "duck", "dubber ruck", "second opinion", "sanity check", "rubber duck", or "what am I missing". It gets a second opinion from a model you host yourself (nothing leaves your network) via the dubber-ruck CLI - review a diff, consult with files, rubber-duck a problem, or check a plan. Small local models are right most of the time but not always, so findings are hypotheses to verify, never facts to relay.
when_to_use: Fresh project starting with a planning round; you just wrote a plan or PLAN.md; you are about to git commit; you have retried a fix twice; you are about to delete, migrate, or deploy; the user asks for a second opinion or a duck.
argument-hint: [review|consult|duck|plan] [question, focus, or plan file]
allowed-tools: Bash(dubber-ruck *)
---

# dubber ruck

A second reviewer that is differently wrong from you. It has no memory of how the
work got here, which is exactly why it catches assumption drift you cannot see. It
is a small self-hosted model, so it is also wrong a meaningful fraction of the time,
with characteristic failures: invented APIs, invented framework behaviour, and
confident claims about lines that are not in the diff. The tool and this skill exist
to make that useful instead of dangerous.

## Server state right now

```
!`dubber-ruck status 2>&1 || true`
```

A self-hosted server may have a single slot shared with other people. If the verdict
above is `busy`, someone else is mid-request: wait for a natural pause or skip the
consult. If it is `unreachable`, skip it and say so in one line. Do not poll or retry
in a loop.

## When to consult

Unprompted, at these checkpoints:

- Before committing a diff that is more than a few lines and not mechanical.
- After drafting a plan that touches three or more files or makes a design choice.
- When the same bug has survived two attempted fixes.
- Before anything destructive or hard to reverse: migrations, deletions, deploys,
  production config.

On demand whenever the user asks for a duck, a second opinion, or a sanity check.

Two of these checkpoints are also enforced by hooks in `~/.claude/settings.json`:
a `git commit` triggers a quick review of the staged diff, and leaving plan mode
(`ExitPlanMode`) triggers a plan check. When a hook has already run, its output
arrives as additional context; do not run the same review again by hand. Plans
written outside plan mode (a PLAN.md you drafted in conversation) are not caught by
the hook, so run `dubber-ruck plan` on those yourself before presenting them.

Do not consult for typo fixes, comment edits, config value changes, renames, or
anything the user needs in the next minute. Do not consult more than once per
checkpoint. Never send the whole repository: the diff, or the two or three files
that matter, plus a focused question.

## How to run it

```
dubber-ruck review [--staged | --range A..B | --commit REV] [--focus "..."]   # diff vs HEAD by default
dubber-ruck consult "specific question" -f file1 -f file2
dubber-ruck duck "what I am stuck on, in my own words"
dubber-ruck plan PLAN.md [-f context-file]        # fixed checkable questions; the CLI decides the verdict
dubber-ruck consult ... --dry-run                 # input size and time estimate, nothing sent
```

`--votes 3` on `review` or `consult` samples three times and keeps only findings a
majority agree on. Three times slower; use it only when a decision hinges on whether
something is really a bug, and never for a routine pre-commit look.

Use `plan` after you have drafted a plan of three or more files or any irreversible
step, before executing it. Write the plan to a file first. Its verdict is computed from
fixed questions, so "READY" means "no concern found", not "approved".

Cost: `review` and `consult` think by default and take 2 to 5 minutes. `duck` does
not think and answers in about 25 seconds. Two ways to run a thinking call:

- **You have other work to do meanwhile**: run it with `run_in_background`, keep
  working, and fold the result in when the notification arrives.
- **You would otherwise be idle** (the user is waiting for exactly this answer, or it
  is the last thing before you reply): run it in the **foreground** with the Bash
  `timeout` raised to 600000. Never end your turn with a promise to report the duck's
  result later; a turn that ends is a result that is lost.

Add `--no-think` for a quick answer to a simple question. `-q` suppresses progress
lines. The command refuses to run when this session is itself pointed at the same
local server (`ANTHROPIC_BASE_URL`); do not add `--force` to get around that.

If `$ARGUMENTS` is given: a first word of `review`, `consult`, `duck`, or `plan` picks
the mode and the rest is the question, focus, or plan file. Otherwise, if there is an uncommitted
diff, run `review` with the arguments as `--focus`; if not, run `duck` with them.

## How to treat what comes back

Findings look like this:

```
- [confidence 4/5] [grounded] main.go:138 `m.spun += 0.05` — spinSpeed is never read. Verify by: grep spinSpeed
```

The first tag is the model's confidence. The second is mechanical: `[grounded]`
means the quoted line really occurs in what was sent, `[UNGROUNDED ...]` means it
does not, `[unquoted]` means there was nothing to check.

Rules, in order of importance:

1. **Verify before acting.** For each finding, do the "Verify by" step or read the
   line yourself. A finding you have not checked is a question, not a result.
2. **Discard `UNGROUNDED` findings** unless you independently confirm the claim.
   Quoting a line that is not there is this model's signature hallucination.
3. **Treat confidence 1 or 2 as a question** to answer, never as a defect to fix.
4. **Your verified knowledge and the user's instructions win.** If the duck
   contradicts something you have checked, say so and move on. Never revise a
   decision the user made because the duck disagreed with it.
5. **No rubber stamps either way.** "SHIP" from the duck is not approval; it means
   it found nothing, with a 25% chance of having missed something real.

## How to report it

One short paragraph in your next message to the user: what you asked, what the
duck flagged, and what survived your verification. Attribute it by name and keep
the two voices separate:

> dubber ruck flagged the range loop in `Update()` as mutating a copy; I checked
> and it is right, fixed. It also claimed `rand.Seed` is required; that is wrong
> on Go 1.22, ignored.

Never present a duck finding as your own observation, and never present an
unverified one as fact. If the duck was busy or unreachable, one line saying so
is enough; continue without it.

## Exit codes

0 ok · 1 error · 2 unreachable · 3 slot busy · 4 refused (would swap models, or
self-consult) · 5 empty output · 6 input too large. On 3, 4, and 6 do not retry
with flags that override the guard; they protect other people using the box.
