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

## You are not on macOS

Everything below was found on macOS, and that is the only platform this has
been run on. Nothing is deliberately macOS-only, but three things look there
first and have not been tried elsewhere:

- **Finding a CLI.** Claude Code is looked for inside `claude.app` and then on
  `PATH`; Codex under `~/.codex`; Antigravity inside `/Applications`. The
  `PATH` lookup is the portable one and should work anywhere.
- **Keeping the machine awake** uses `caffeinate`, and does nothing at all off
  macOS — including saying so, beyond one line in the log. A Linux box that
  suspends will take the gate down the same way a Mac did.
- **Nothing has been run on Windows.** The hooks are shell scripts.

`halyard doctor` is the first thing to run, and the place a wrong assumption
about where something lives will show up.

## A message never arrives, and the log says nothing useful

**`Delivering a message to … failed (exit 1):` with nothing after the colon.**

That was Halyard reading only stderr while the CLI wrote its reason to stdout.
Fixed: the reason now travels to the phone with the failure, so the card says
what happened rather than telling you to read a log on a machine you are away
from.

## Deliveries stop with "OAuth session expired and could not be refreshed"

The gate still works, the card still arrives, and every message you send from
your phone comes back as:

```
⚠️ That did not reach <session> (claude-code).
Failed to authenticate: OAuth session expired and could not be refreshed
```

The login `/login` creates is refreshed while somebody is at the keyboard, and
eventually cannot be. Measured on one machine twice, four days apart — each time
it stopped remote work until somebody signed in at the desk, which is precisely
where you are not.

The fix is to give the control plane a credential of its own:

```bash
claude setup-token
```

That mints a long-lived token — it uses your **subscription** rather than
pay-as-you-go API billing, and lasts about a year. Put it in `halyard.yaml`:

```yaml
settings:
  HALYARD_CLAUDE_OAUTH_TOKEN: "sk-ant-oat01-…"
```

It is a secret, and lives in the same gitignored file as the bot token. Only the
turns *Halyard* starts use it; a session you drive at the keyboard is untouched.

`halyard doctor` warns whenever this is unset, and names what the CLI is falling
back to. Two things it cannot do: `claude auth status` carries no expiry — only
`loggedIn`, `authMethod`, `apiProvider`, measured on 2.1.246 — so nothing can
warn you *before* it lapses; and if `ANTHROPIC_API_KEY` is set anywhere in the
environment it outranks the token **and bills the API instead of your plan**, so
`doctor` says that too.

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

## The service is installed and launchd says it does not exist

```
Could not find service "com.halyard.fleet" in domain for user gui: 501
```

The plist is in `~/Library/LaunchAgents`, `halyard service install` reported
success, and `launchctl` will not admit the service exists at all. Nothing about
the file is wrong.

Something ran `launchctl unload -w`, which is what people reach for to stop the
service while working locally — and this project's own instructions said so for
a while. The `-w` writes a **persistent disabled record** into launchd's own
database, separate from the plist, and the legacy `load -w` does not reliably
undo it. Reinstalling looked like it worked every time.

```bash
launchctl print-disabled gui/$(id -u) | grep halyard    # the evidence
uv run halyard service install                          # clears it and starts
```

`install` now enables the label before it bootstraps, so this cannot recur, and
`halyard service status` reports the disabled state by name rather than as
"not loaded". To stop it for local work, use the command that leaves no record:

```bash
launchctl bootout gui/$(id -u)/com.halyard.fleet
```

---

## What is worth checking before asking anywhere else

```bash
make doctor                                    # the whole chain, in order
pmset -g assertions | grep caffeinate          # macOS: is it holding the machine awake
tail -20 logs/halyard.log                      # what the control plane was doing
tail -20 logs/bridge-$(date +%G-W%V).log       # what the hooks did, including the
                                               # ones it never heard about
```

The bridge log is the one place a hook that never reached the control plane
leaves a trace. It is where several of the entries above were first seen.

**Logs live in `logs/`, a new file each week.** One flat file reached thirty-six
thousand lines in a month, which is a file you grep and hope rather than a log
you read. `logs/halyard.log` is always the current week and past weeks sit
beside it as `halyard.log.2026-W34`; the hooks write their own
`bridge-2026-W35.log` into the same folder, so everything about one week is in
one place. Eight weeks are kept — `HALYARD_LOG_BACKUPS` changes that — and a
week noisy enough to threaten the disk rolls early rather than growing until
Monday.

If you are on an installation from before this, the old flat `halyard.log` and
`bridge.log` stay exactly where they are; nothing is moved. New lines simply go
to the folder, and the line the control plane prints at startup says where.

The service's own log is separate and is launchd's, not Halyard's:

```bash
tail -20 ~/Library/Logs/halyard-service.log    # git pull, uv sync, and the console
```

It holds everything the process printed to its console, so it repeats much of
`logs/halyard.log` and grows without bound. Nothing rotates it — truncate it
when it gets big:

```bash
: > ~/Library/Logs/halyard-service.log
```
