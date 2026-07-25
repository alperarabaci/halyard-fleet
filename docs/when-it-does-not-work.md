# When it does not work

Every way installing this has gone wrong so far, on two machines, with the fix.

They have one shape in common: **nothing fails loudly.** The gate fails closed,
so a broken setup looks exactly like a working one refusing you, and a broken
delivery looks like an agent that has gone quiet. `halyard doctor` now catches
most of what is below — it was taught each of them the day it happened.

Start there:

```bash
make doctor
```

---

## A message never arrives, and the log says nothing useful

**`Delivering a message to … failed (exit 1):` with nothing after the colon.**

That was Halyard reading only stderr while the CLI wrote its reason to stdout.
Fixed: the reason now travels to the phone with the failure, so the card says
what happened rather than telling you to read a log on a machine you are away
from.

## The CLI is installed and says "Not logged in"

`claude` runs, prints `Not logged in · Please run /login`, and every delivery
fails. The binary is fine; the session is not.

```bash
make doctor          # prints the exact path, quoted
```

Then paste the line it gives you. **Do not just type `claude auth login`** —
that command frequently does not exist in your shell on a machine that has
Claude Code, because the binary lives inside the desktop app's bundle and
nothing is published to `PATH`. That is also how Halyard finds it, and why the
shell does not.

## A CLI installed after the control plane started is invisible

It used to be: the binary was located once, at startup. Install `codex` while
Halyard is running and it stayed unfound until a restart — with `doctor`
reporting it present, because `doctor` had asked just now and the control plane
had asked at breakfast.

Fixed by resolving the path per call. The same bug had a quieter version:
Claude Code's binary lives under a version number, so an upgrade moves it and a
long-running control plane keeps pointing at a path that no longer exists.

## The machine goes to sleep and everything stops

A control plane that sleeps does not pause — a wired project cannot run a
command without an answer from it. The symptom is approvals quietly appearing
in the desktop app instead of on your phone.

Measured on a Mac mini: its only two wake assertions belonged to a
screen-sharing window. Closing that window put the machine to sleep. Halyard
now holds an idle-sleep assertion while serving, tied to its own process:

```bash
pmset -g assertions | grep caffeinate     # should be there while it runs
```

`HALYARD_KEEP_AWAKE: false` turns it off. Display sleep, the lid, and choosing
Sleep from the menu are all untouched.

## Hooks from another machine appear in the project

A project ends up with two `PreToolUse` groups on one matcher, half of them
naming `/Users/<somebody-else>/…`.

`.codex/hooks.json` and `.agents/hooks.json` hold **absolute paths**, so a
committed one travels by git and lands on a machine where those paths do not
exist. Each machine's `wire` then correctly decides the other's entry is not
its own — and appends beside it.

`make wire` now drops a Halyard hook that cannot run on this machine. Fix the
cause too:

```bash
printf '.codex/hooks.json\n.agents/hooks.json\n' >> .gitignore
git rm --cached .codex/hooks.json     # leaves it on disk; the gate keeps working
```

`doctor` warns about both states: committed, and not yet ignored.

## Codex runs the hook and nothing happens

Codex will not run a hook it has not been told to trust, **and it does not say
so.** An untrusted hook is skipped in silence: the turn completes, nothing is
printed, and a `PreToolUse` gate that is not trusted is not a gate.

```bash
cd <project> && codex          # a hook review appears at startup
```

Prefer `Review hooks` over `Trust all` — these run from outside the project.
Trust covers a hash of the handler, so updating this checkout can revoke it;
`doctor` reports when trust looks stale.

## The wrong project gets gated

`halyard wire` with no argument used to mean "here", and "here" is almost
always the Halyard checkout — so the control plane's own commands went through
the hook it was serving. It reads `halyard.yaml` now. If you gated the checkout
before that, take it off explicitly:

```bash
uv run halyard unwire .
```

## Antigravity asks twice

Approve on your phone, and the desktop app asks again. This is why Antigravity
is not in the supported list: a `run_command` that bypasses the sandbox is
gated by something no hook can answer, and Antigravity ignores an override that
tries — deliberately, because honouring one would let whoever controls the hook
run unsandboxed code on the machine.

The measurements are in [Antigravity notes](antigravity-payload-notes.md).

---

## What is worth checking before asking anywhere else

```bash
make doctor                               # the whole chain, in order
pmset -g assertions | grep caffeinate     # macOS: is it holding the machine awake
tail -20 bridge.log                       # what the hooks did, including the ones
                                          # the control plane never heard about
```

`bridge.log` is the one place a hook that never reached the control plane
leaves a trace. It is where several of the entries above were first seen.
