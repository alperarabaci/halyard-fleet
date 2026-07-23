# Blameless Postmortem: Claude Desktop Live Session Synchronization Regression

**Date:** 2026-07-23  
**Status:** Corrective changes implemented locally; manual live-view verification pending  
**Affected path:** Telegram-to-Claude message delivery for sessions already open in Claude Desktop

## Summary

Messages sent from Telegram continued to the correct persisted Claude Code
session, but stopped appearing immediately in the already-open Claude Desktop
task. Closing and reopening Claude Desktop caused the missing turns to appear,
which initially made the symptom look like an unsupported desktop-refresh
boundary rather than a regression.

The investigation found two changes between the known-working and failing
paths:

1. Halyard launched the standalone Claude Code `2.1.216` binary while Claude
   Desktop owned the open task with its bundled Claude Code `2.1.217` engine.
2. Halyard had begun forcing `--model sonnet` on every remote resume. The
   known-working resume passed no model flag and inherited the open session's
   `claude-opus-4-8` model.

The second change came from a real but inapplicable measurement: a fresh
headless `claude -p` prompt defaulted to haiku. That result was generalized to
`claude -p --resume`, even though a resumed Desktop-owned session had already
demonstrated different behavior.

No session data was lost, no message was delivered to a different Claude
conversation, and no approval was bypassed. The turns were present in the
original transcript with a continuous parent chain.

## Impact

- A Telegram message could complete successfully and be persisted in the
  intended Claude session without appearing in the task currently visible on
  the desktop.
- The user had to close and reopen Claude Desktop to see the remote turn.
- Remote turns silently ran on Halyard's forced Sonnet model instead of the
  model selected in the resumed session.
- The visible symptom weakened confidence in session identity and made a
  successfully persisted turn look lost.

## Detection

The regression was detected during a manual Telegram-to-Desktop test. The
control plane reported successful delivery and the reply returned through
Telegram, but the open Claude Desktop task did not update.

Transcript inspection separated persistence from presentation:

- The Telegram message existed in the original named session.
- Its `parentUuid` continued the existing conversation lineage.
- The response followed it in the same transcript.
- Reopening the desktop task displayed the previously absent records.

## Timeline

- **2026-07-21:** A Telegram message was resumed into a session already open in
  Claude Desktop. The app displayed the turn live. The transcript recorded
  Claude Code `2.1.215`, entrypoint `claude-desktop`, no forced model, and an
  assistant response from `claude-opus-4-8`.
- **2026-07-22:** Halyard added a default `--model sonnet` to avoid the haiku
  default measured on a fresh headless prompt.
- **2026-07-23:** Remote resumes were recorded with standalone Claude Code
  `2.1.216`, entrypoint `sdk-cli`, and `claude-sonnet-5`. Claude Desktop owned
  the open task with its bundled Claude Code `2.1.217`.
- **2026-07-23:** The persisted transcript and parent chain were verified,
  ruling out wrong-session delivery, a silent fork, and message loss.
- **2026-07-23:** Halyard was changed to prefer Claude Desktop's bundled engine
  on macOS and to omit `--model` unless the user explicitly chooses an
  override.

## Root Causes

### Runtime version selection

The runner selected the first `claude` executable found on `PATH`. For a
service launched outside the desktop application, that was the standalone CLI,
even when Claude Desktop had a newer bundled engine actively owning the target
session.

This made two independently updated Claude Code builds cooperate through one
persisted transcript. That happened to work for durable history but regressed
the live behavior previously observed in the desktop client.

### Fresh-prompt behavior was applied to resume behavior

The default-model change was based on a correct measurement of the wrong
operation. A fresh `claude -p` invocation used haiku when no model was supplied.
Halyard therefore forced Sonnet to avoid silently downgrading remote work.

The message runner does not create fresh prompts; it resumes an existing
session. The known-working transcript showed that a resume with no model flag
inherited the open session's Opus model. Adding the default changed a
load-bearing part of the working command and replaced the user's session
choice.

## Contributing Conditions

- The original live-display proof was documented as an outcome, but its binary
  source, version, entrypoint, and absence of a model flag were not captured as
  regression assertions.
- Fresh headless prompts and resumed sessions shared one CLI surface and were
  treated as though their defaults were interchangeable.
- The runner had no observability showing which Claude executable it selected.
- Durable transcript persistence was easier to test automatically than live
  rendering in an already-open proprietary desktop client.
- The Codex adapter and shared routing changes were being investigated at the
  same time, making a Claude-specific regression initially look like a common
  desktop synchronization limitation.

## What Went Well

- The message remained in the intended session and could be recovered by
  reopening it.
- The transcript contained enough version, entrypoint, model, and lineage
  information to distinguish a UI refresh problem from data loss.
- The existing Claude runner boundary allowed the correction to remain
  Claude-specific; Codex delivery and shared channel routing did not need
  another structural change.
- The explicit `/model` feature remains available for users who intentionally
  want to replace a session's model.

## Corrective Actions

### Implemented locally

- Prefer the newest Claude Code engine bundled with Claude Desktop on macOS.
- Retain standalone CLI discovery as a fallback on systems without the desktop
  bundle.
- Add `HALYARD_CLAUDE_BINARY` as an explicit escape hatch.
- Stop supplying a default `--model` for resumed Claude sessions.
- Preserve `/model` and `HALYARD_CLAUDE_DEFAULT_MODEL` as explicit overrides.
- Make `/model default` restore session/runtime inheritance.
- Update `/status` to say when remote turns inherit the resumed
  session/runtime.
- Make `halyard doctor` print the selected Claude executable.
- Add regression tests for desktop-binary preference, explicit binary
  overrides, session-model inheritance, and configuration propagation.

### Verification completed

- The selected executable resolves to Claude Desktop's bundled Claude Code
  `2.1.217` engine on the affected machine.
- The full automated suite passes: **496 tests**.
- Ruff lint and formatting checks pass.
- `git diff --check` passes.
- A live `halyard doctor` run reports all configured seats, hooks, Codex trust
  records, and the selected Claude executable as healthy.

### Verification still required

- Restart the Halyard control plane so the updated runner is loaded.
- Keep a Claude Desktop task open.
- Send a message from that task's Telegram group.
- Confirm that the turn appears without closing or reselecting the task.
- Confirm that the response uses the task's existing model when no Halyard
  model override is configured.

The manual result must be added here before the live-refresh correction is
described as verified end to end.

## Prevention and Follow-up

- Record executable source, runtime version, entrypoint, model flags, and
  session lineage in future session-I/O measurements.
- Keep fresh-prompt and resumed-session behavior as separate contracts.
- Treat a default added to a resume command as a compatibility change, even if
  the same flag is harmless for a fresh invocation.
- Continue exposing selected runtime executables through diagnostics.
- Keep the manual open-desktop smoke test in release checklists until the
  desktop client offers an automatable supported surface for it.

## Lessons

The important distinction was not documentation versus implementation; it was
one measured operation versus another. A true result for a fresh prompt became
a false assumption for a resumed session because both commands looked almost
identical.

Blamelessly, the change optimized for a real quality risk and had tests proving
the command it intended to build. The missing test was the boundary that
actually mattered: preserving the semantics of an already-open session. Future
changes should describe and test that boundary directly.
