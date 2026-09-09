# Blameless Postmortem: A Permission Prompt Nobody Was There to Answer, and an Investigation That Blamed the Wrong Component

**Date:** 2026-09-09
**Status:** Fixed in the affected machine's Docker configuration and verified by revoking the grant. No change to Halyard.
**Affected path:** project commands run from the phone — `bootstrap: make bootstrap-up`

## Summary

Running a project command from Telegram on the Mac mini put a macOS dialog on
that machine's screen:

> **"uv" would like to access data from other apps.**   *Don't Allow* / *Allow*

Nobody was at the machine, which is the entire premise of running the command
from a phone.

What the dialog costs when it is not answered was established afterwards, in a
terminal, by deliberately declining it. The call behind it blocked until
Docker's own deadline and the build died:

```
#6 [mcp internal] load metadata for docker.io/library/python:3.14.6-slim-bookworm
#6 ERROR: DeadlineExceeded: context deadline exceeded
...
target mcp: failed to solve: DeadlineExceeded: context deadline exceeded
```

The cause is one line in `~/.docker/config.json`:

```json
"credsStore": "desktop"
```

With that set, every registry contact runs `docker-credential-desktop`, which
reads Docker Desktop's own application data. macOS guards that behind
`kTCCServiceSystemPolicyAppData`, and the prompt names whichever process it
holds responsible — `uv`, because the whole chain descends from
`uv run --package alpha-bootstrap python -m bootstrap up`. The binary being
asked about was not the binary doing the reading.

On that machine `auths` was empty, there were no `credHelpers`, and every image
in the build is public. The helper was being consulted for nothing, and the
only thing it could contribute was a dialog that could stop the build.

**Nothing in Halyard was wrong**, and nothing in Halyard was exercised by the
failure either: the run that started from the phone was approved by somebody
who happened to be near the machine, so it succeeded. This postmortem is mostly
about how long it took to establish where the fault was not.

## Impact

- An unattended build failed, in the one circumstance the feature exists for.
  A prompt on a screen nobody is looking at is not a prompt; it is a timeout
  with an explanation that never leaves the room.
- The failure is intermittent by construction. macOS asks once and remembers,
  so the prompt appears only when there is no recorded decision — which is
  exactly why "it worked yesterday" was true and useless.
- It will recur. The grant is bound to `uv`'s identity, and `uv` has none of
  its own: TCC recorded `identifier=uv-e8ea3f43d4703993`, derived from the
  binary. Every `uv` upgrade invalidates the grant and asks again.

## What actually caused it

The log named the permission and the subject exactly, and both halves matter:

```
service=kTCCServiceSystemPolicyAppData
responsible_path=/Users/…/.local/bin/uv
```

`service` says which door. `responsible_path` says who macOS bills it to, and
TCC bills a whole process tree to its responsible ancestor. Under a control
plane started with `uv run`, that ancestor is `uv` for everything it spawns —
`make`, `docker`, a credential helper five processes down. The name in the
dialog is therefore evidence about the *tree*, not about the access, and
reading it as "uv touched something" is reading it as the opposite of what it
says.

## Why it took eight exchanges

This is the part worth keeping.

**A machine-level fact was measured on the wrong machine, twice.** Docker was
eliminated as a suspect because the development Mac's socket sits at
`~/.docker/run/docker.sock`, outside any protected directory. The mini's socket
is in the same place — that check was even run there — so the elimination
looked sound. It was not, because the socket was never the access; the
credential helper was, and nothing about the socket's path says anything about
that. Earlier the same day, a reset time in a quota alert was called a timezone
bug on the strength of the development Mac's service logging local time. Same
error, six hours apart: a fact that does not travel between machines was
measured on one and concluded for the other.

**Elimination was reported as proof.** The project's source was grepped for
`Application Support`, `Library/Containers` and `Group Containers`; nothing
matched; the conclusion drawn was that the trigger could not be the project's
command. But the path in question never appears in that project's source — it
comes from `~/.docker/config.json` at runtime, by way of a helper binary chosen
by a configuration key. **A grep for literals cannot find a path supplied by
configuration**, and treating an empty result as an absence produced a
confident answer with nothing under it.

**A true fact was mistaken for a confirming one.** Halyard really does read
another application's data: `find_claude_binary()` globs
`~/Library/Application Support/Claude/claude-code` on every lookup, and
`halyard doctor` on the mini confirmed that both Claude Code seats resolve
through it. Every word of that is correct, and none of it is evidence about
this dialog. It established that the access exists, and was read as
establishing that it was the cause — a substitution that survived several
exchanges precisely because each new check came back true.

**The person reporting it said it was unrelated, and was argued with.** The
sharpest evidence in the whole investigation arrived as a sentence: *"alpha
engine does nothing with Claude."* It was answered with more elimination. What
finally settled it was the same person adding that the dialog appears when the
command is run from VS Code — which removes Halyard from the chain entirely and
falsifies the hypothesis in one line. **The user's account of their own system
was better evidence than anything measured remotely, and it was the last thing
consulted rather than the first.**

## What settled it

Four steps, none of which required a guess:

1. **Reproduce outside the suspect.** From VS Code, with no Halyard anywhere in
   the chain — the prompt still appeared.
2. **Bisect the command.** `make bootstrap-up` is two halves; running them
   separately put the prompt in the second.
3. **Read where it stopped.** The dialog arrives at `load metadata for
   ghcr.io/...` and `resolve image config for docker.io/...` — the first
   registry contact, which is the first credential lookup. Not answering it
   fails *that* step and no other.
4. **Revoke the grant and rerun.** After removing `credsStore`,
   `tccutil reset SystemPolicyAppData` put the machine back in the state where
   it would have to ask, and the build then ran without asking. That is the
   step that turns a plausible cause into a demonstrated one, and it is the
   step it is most tempting to skip once something starts working.

Step 4 also disposed of a test that proves nothing: rerunning the build after
clicking *Allow* succeeds whether or not the fix is real, because the grant is
already in place. Restarting the editor does not help either — TCC decisions
live in a system database and survive restarts and reboots.

## What changed

**On the affected machine, and nowhere else.** `credsStore` was removed from
`~/.docker/config.json`, so no credential helper runs and no application data is
read. Verified as above.

**Nothing in Halyard**, and the honest reason is not that the command path was
proven good here — it was never put in that position. The phone's run was
approved and succeeded, so what reached the phone was a success report. The
failure output quoted at the top came from a terminal, and an early draft of
this document said it had reached the phone. It had not. That sentence was
written the same way as the rest of the wrong answers below: an inference about
what the code would do, recorded as an observation of what it did.

What can be said about the command path is what the code says. On failure it
carries the last twenty-five lines, ANSI stripped, which is enough to hold the
`failed to solve:` line at the end of a buildkit failure; on success it carries
five, because "it passed" is the message; and it reports progress every
forty-five seconds, so a run blocked behind a dialog would have read as a long
run rather than as silence. None of that was tested by this incident.

Deliberately not built: a check that understands macOS permission prompts.
`command` runs whatever a project puts in `halyard.yaml`, and teaching it about
one operating system's permission model to catch one failure that is now fixed
at its source would trade a general mechanism for a specific one. If the same
shape recurs in another form, the generic version of it is "nothing has been
printed for N minutes", which is worth building when there is a second case and
not before.

Two things left as notes rather than changes:

- **Docker Desktop rewrites `config.json` on update.** If the prompt returns,
  `credsStore` is the first thing to check. The durable form is a separate
  `DOCKER_CONFIG` directory for that make target, which Docker Desktop does not
  touch.
- **`tccutil reset SystemPolicyAppData <identifier>` does not work for `uv`.**
  It has an ad-hoc identity rather than a bundle id, so only the unscoped reset
  applies — which also resets every other client of that service.

## What to carry forward

- **Configuration is not code, and grep only reads code.** A path that arrives
  through a config key, a credential helper, a symlink or an environment
  variable is invisible to a search of the source. An empty grep is the absence
  of a literal, not the absence of an access.
- **Machine-level facts do not travel.** Launchd environments, TCC grants, CLI
  versions and Docker configuration differ between the two machines by nature.
  Measuring one and concluding for the other has now been wrong twice in a day.
  Say which machine a measurement came from, every time.
- **Eliminating candidates is not identifying one.** "Everything else is ruled
  out" is only as strong as the enumeration, and the enumeration here was a
  grep that could not see the answer. A cause is established by making the
  symptom appear and disappear on demand, which is what step 4 finally did.
- **A confirming fact is not a cause.** Halyard reading another app's data was
  true, checkable, and beside the point. Each true detail made the wrong answer
  feel better supported, which is how a hypothesis survives contact with
  evidence that never actually tested it.
- **Write down what was observed, not what would follow.** The first draft of
  this document claimed a failure message had reached the phone. It was a
  reasonable inference from the code and it was not what happened, and it was
  caught by the person who had been looking at the phone. A postmortem about
  asserting instead of measuring is the last place that should appear, which is
  the argument for it applying to the writeup as much as to the investigation.
- **The person at the machine is the primary instrument.** They can see which
  applications are installed, what they run and where. When they say a
  component is not involved, that is a measurement from the only vantage point
  that has the whole system in view — and it belongs at the start of the
  investigation, not at the end of it.
- **TCC prompts and unattended work are incompatible.** Anything a control
  plane runs on behalf of somebody who is away must not depend on a dialog. The
  general form is worth remembering beyond Docker: the first time a new binary
  in the chain touches a guarded location, it asks, and asking is failing.
