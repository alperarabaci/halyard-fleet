# Blameless Postmortem: CI Hung for Fourteen Minutes and Produced No Information

**Date:** 2026-07-25
**Status:** Diagnosis gap closed; the cause itself was never established
**Affected path:** The `test` job — the only check that gates a merge into `main`

## Summary

A pull request's CI run reached 32% of the test suite and then produced nothing
for eight and a half minutes. A person watching the spinner cancelled it. The
whole log, from the last useful line onwards, was:

```
20:47:33  .......FF..F.F.....F.F.................................. [ 10%]
20:47:54  ........................................................ [ 21%]
20:48:02  ........................................................ [ 32%]
20:56:35  ##[error]The operation was canceled.
20:56:35  Terminate orphan process: pid (2323) (uv)
20:56:35  Terminate orphan process: pid (2326) (pytest)
```

An earlier attempt had been cancelled the same way at seven minutes. The suite
takes forty-three seconds locally.

Two things were wrong, and only one of them is understood.

**The failures are understood.** Runners had just been changed to resolve their
CLI path when they need it rather than once at startup — a real fix, for a
control plane that could not see a CLI installed after it started. A
consequence of resolving later is that a configured path is now checked for
existence. Two test helpers were still configuring runners with macOS-only
paths: an Antigravity `.app` bundle and a Homebrew `codex`. Neither exists on
an Ubuntu runner, so those runners had no binary and their delivery tests
failed.

**The hang is not.** It did not reproduce after those failures were fixed, and
both changes shipped together, so the two cannot be separated. It may have been
a consequence of the failures, or environmental, or still present and waiting.
Nothing here claims otherwise.

## Impact

- Two CI runs were cancelled by hand, roughly fifteen minutes of wall time each.
- The branch could not merge: `main` requires this check, correctly.
- Most of the cost was not the hang. It was that the hang was **unanswerable** —
  no test name, no traceback, no timing beyond "somewhere after 32%". The only
  available next step was to guess, and two guesses were made and discarded
  before the real approach was taken.

## What went wrong

**A test suite with no timeout has no failure mode for blocking.** Every other
way a test can go wrong produces a name and a traceback. Blocking produces
progress that stops, which is indistinguishable from a slow machine until
somebody decides how long is too long — and that decision was being made by a
person, in a browser, watching a spinner.

**The environments agreed on everything except the operating system.** `uv sync
--frozen` pinned identical versions on both sides — `pytest 9.1.1`,
`pytest-asyncio 1.4.0`, all thirty-four packages. That is worth stating because
it eliminated the usual first suspicion and left the difference nobody controls:
CI is Linux, development is macOS, and the only tests that knew the difference
were the ones asserting about paths.

**A local run cannot answer a question about CI.** Reproducing the CI condition
locally — hiding the agent CLIs from `PATH`, pointing `HOME` elsewhere — was
tried, and the suite passed in forty-five seconds. That result is worth exactly
nothing about the hang, and treating it as reassurance would have been the
mistake this repository has written down twice already.

## What changed

1. **`pytest-timeout`, at sixty seconds, with `timeout_method = "thread"`.** The
   suite runs in forty-three, so anything reaching the limit is stuck rather
   than slow. The thread method rather than the signal one because most of this
   suite is async, and a signal does not interrupt a blocked `await`.
2. **The two macOS-only paths are real executables now**, matching what the
   Claude Code test already did after the same change.
3. Proven rather than assumed: a deliberate `time.sleep(120)` inside the suite
   now fails at sixty seconds and names itself. The first attempt at that proof
   was invalid — the probe was written to `/tmp`, which moved pytest's rootdir
   and made it ignore the project's configuration entirely, so the sleep ran to
   completion and "passed". A proof that runs somewhere the setting does not
   apply proves the opposite of what it looks like.

## What to carry forward

- **Every automated check needs a bound.** Not because tests hang often, but
  because the one time it happens the cost is not the delay — it is that there
  is nothing to act on, and the next move is a guess.
- **An unreproduced cause stays unreproduced.** The hang is not explained here
  and is not written up as though it were. If it returns, the timeout will name
  it, and that entry can be written then.
- **Check where a proof is running.** A test that demonstrates a configuration
  works has to run under that configuration; `/tmp` was a different rootdir and
  a silently different answer.
