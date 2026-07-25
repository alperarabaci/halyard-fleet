# Blameless Postmortem: `halyard wire` Gated a Project With a Runtime It Was Never Configured For

**Date:** 2026-07-25
**Status:** Corrective changes implemented; live verification on the affected machine pending
**Affected path:** `halyard wire` / `halyard unwire` — which runtimes a project is gated for

## Summary

A second machine (a Mac mini) had a working Halyard setup: Claude Code seats,
approvals arriving in Telegram, commands being allowed and denied from the
phone. Its `halyard.yaml` described exactly two seats, both `claude-code`.

After pulling a new revision and running `halyard unwire` followed by
`halyard wire alpha-engine`, that project was left with:

- **Antigravity's hooks installed**, for a runtime that appears nowhere in its
  configuration, and
- **no Claude Code hooks at all**, for the only runtime it actually uses.

The command reported success. Its entire output about the runtimes was one
line — `antigravity: already wired` — and the closing paragraph said the
project was now gated and to restart the session.

The cause is a single decision made in the wrong place. `wire` chose which
runtimes to install by asking **the machine** what was installed, and never
read the configuration that says which runtimes the project uses:

```python
chosen = runtimes if runtimes is not None else installed() or RUNTIMES[:1]
```

On that machine `installed()` answered `(antigravity,)` — Antigravity publishes
an `agy` command, and `on_this_machine()` for Claude Code was
`shutil.which("claude")`, which finds a shim on `PATH` and knows nothing about
the binary Claude Code ships inside `claude.app`. So the one runtime the
project was configured for was invisible, and the one it had never heard of was
what got written.

No approval was bypassed and nothing was deleted: `unwire` had already removed
Halyard's own hooks, and `wire` wrote a backup of every file it touched. What
was lost was the gate itself, on a machine where it had been working.

## Impact

- A project configured for Claude Code was left ungated for Claude Code.
  Because the gate is the thing that asks, an ungated project does not fail —
  it runs commands without asking anyone, which is indistinguishable from
  working normally right up until you look for the approval that never came.
- The same project was gated for a runtime nobody uses there. Harmless in
  effect and misleading in appearance: `.agents/hooks.json` is evidence that
  somebody configured Antigravity for this codebase, and nobody did.
- The command's success output stated the project was gated. A person who read
  it had no reason to check.

## What went wrong

**The configuration was never consulted.** `halyard.yaml` says which runtimes
work in which project — that is most of what it is for — and the code that
decides what to gate a project with did not open it. It asked a question whose
answer is about the machine ("what is installed here?") and used it to answer a
question about the project ("what does this codebase use?").

**The two questions disagree in both directions, and both happened at once.**
A runtime installed but unconfigured got a gate. A runtime configured but not
detected did not. Either alone would have been a bug; together they produced a
result with no correct part in it.

**Detection of an installed runtime was a `PATH` lookup.** Antigravity already
had an exception to this, because its binaries live inside `Antigravity.app` —
so the failure mode was known, written down in that runtime's own spec, and not
generalised to the runtime with the same property. Claude Code ships its binary
inside `claude.app` too.

**The failure was silent in the shape this project keeps meeting.** Nothing
printed for `claude-code`. Absence of a line is not something a person reads as
a failure, particularly under a closing message that says the project is now
gated.

## Why the tests did not catch it

This is the part worth keeping.

Seven tests called `wiring.wire(project)` with no explicit runtimes, so the
defaulting logic *was* executed. **None of them asserted anything about which
runtimes were chosen.** Every one asserted about
`.claude/settings.local.json` — that hooks were added, that
`permissions.allow` survived, that a backup was written — and none asserted
that a runtime the project does not use was left alone.

Worse, those assertions were satisfied for a different reason on every machine
they ran on:

| Where | `installed()` returns | Why `.claude/settings.local.json` existed |
|---|---|---|
| CI | nothing — no agent CLI on a runner | the `RUNTIMES[:1]` fallback, which is Claude Code |
| The development Mac | all three | Claude Code happened to be among them |
| The Mac mini | Antigravity only | **it did not** — but tests are not run there |

So the suite was green in two places for two reasons, and neither reason was
"the code picked the right runtimes". A test that passes because of a fallback
it is not testing is not evidence about the path it appears to cover.

The general shape, which has now appeared three times in this repository:
**absence is not evidence.** A missing hook, a missing line of output, a
missing assertion. Each looked like nothing happening; each was something
failing.

## What changed

1. **`wire` reads the configuration.** `wiring.configured_for(directory)`
   returns the runtimes named by that project's seats, and that is what gets
   wired. A directory the configuration does not describe still falls back to
   what is installed, because there is genuinely nothing else to go on.
2. **A configured runtime is wired even when its CLI is absent here**, with a
   note saying so. The hooks file is shared; the machine that runs that runtime
   may be a different one. Silence was the wrong answer in both directions.
3. **Each runtime package answers whether it is installed.** Claude Code now
   uses `find_claude_binary()`, which looks inside the app bundle, rather than
   a `PATH` lookup. Codex uses `find_codex_binary()`.
4. **`halyard wire` with no argument reads `halyard.yaml`**, rather than
   defaulting to the working directory. Unrelated in cause and related in
   effect: the working directory when you run `halyard wire` is almost always
   the Halyard checkout, so the old default gated Halyard with its own bridge.
5. **Tests now assert the negative.** That a runtime the project does not use
   is not wired; that a configured runtime is wired even with no CLI present;
   that an installed-but-unconfigured runtime is left alone. These fail on the
   old code.
6. **The machine-dependent tests are pinned.** The ten calls that took the
   default were testing merge behaviour and backups, not the choice of
   runtimes, so they now name the runtime they are about. Writing this
   postmortem is what surfaced them: the fix above changed *which* fallback
   they fell through, and left them just as insensitive to it as before.
7. **One test forces `installed()` to the wrong answer** — Antigravity only,
   which is exactly what the affected machine reported — and asserts that the
   configuration still decides. It is the shortest statement of the rule that
   can fail.

## What to carry forward

- **A decision about a project belongs to the project's configuration.** When
  code answers a question about a codebase by inspecting the machine, the two
  will disagree eventually, and the disagreement will look like success.
- **Assert what must *not* happen.** Every test here checked that the right
  file was written. None checked that a wrong one was not. The bug lived
  entirely in the gap between those.
- **A test whose outcome depends on what is installed is not a test of the
  logic.** If the same assertion can pass through a fallback, through a real
  choice, or through coincidence, it distinguishes none of them. Pin the input.
- **An exception written for one runtime is a fact about the world.** The note
  explaining that Antigravity's binaries hide inside an `.app` was correct and
  sat two files away from Claude Code, which does the same thing.
