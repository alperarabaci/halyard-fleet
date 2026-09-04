# Halyard Fleet

[![CI](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Halyard Fleet puts your coding agent's permission prompt on your phone.

When Claude Code or Codex wants to run something, you see the command, the project it
came from, and how risky it is — then you allow or deny from Telegram. You can also
send new instructions into the running session and read its replies there. And when a
turn dies on a server error the desk would otherwise swallow — an overloaded model, a
usage limit hit mid-run — that reaches you too, instead of the session just going quiet.

It runs on your own machine. No open ports, no exposed API, nothing to log into.

Nothing is approved by accident: every failure — a crash, a timeout, an unreachable
control plane — denies. Two things go through without a person, both empty until you
fill them in: writes to paths you named, and tools you named. Every one is written to
the audit log with the pattern that allowed it.

**Runtimes:** Claude Code, Codex &nbsp;·&nbsp; **Channel:** Telegram &nbsp;·&nbsp; **Tested on:** macOS


<p align="center">
  <img src="assets/demo.gif" width="70%" alt="Approving a command from Telegram" />
</p>


## Why it exists

Remote desktops, terminal streaming and mobile IDEs all try to move *the machine* to
your phone. Halyard moves *the decisions* instead.

> The user should not operate the computer remotely.
> The user should manage the agent's decisions, direction, state, and coordination.

You are not typing commands on a phone. You are answering the questions your agent
would otherwise be blocked on, from wherever you are.

## Quick start

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and keep the token it
gives you. Then message [@userinfobot](https://t.me/userinfobot) to get your own user
id — only ids you list can approve anything.

Create a group for each seat you want to keep separate, and add the bot to each. One
bot covers every group.

### 2. Install

```bash
git clone https://github.com/alperarabaci/halyard-fleet.git
cd halyard-fleet
uv sync --extra dev
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). It runs on the host, not
in a container — it needs the agent CLIs and their credentials.

### 3. Set it up and run

```bash
uv run halyard init      # asks what you have, writes halyard.yaml, wires it, checks it
uv run halyard           # keep this running
```

`init` asks how many seats you have of each runtime, offers the session names it can
already see, and reads the bot token without echoing it. It backs up any file it
replaces and keeps settings it does not manage.

One file describes a machine: the settings and the seats of every project it
gates. `halyard.yaml` is gitignored, and a real environment variable still
overrides it — so a container can pass a token in without writing it to disk.

> **A wired project depends on this process.** With Halyard down, a Bash command in
> that project is *denied* — all of them — and there is no terminal prompt to approve
> it with. `halyard unwire <path>` hands the project back.
> [The rest of what to expect](docs/before-you-wire-it.md) is worth five minutes
> before you walk away from the machine.

Check it any time with `uv run halyard doctor`, and prove the gate actually stops
things with `uv run halyard verify` — which runs real commands into it rather than
reading configuration.

## Commands

| In Telegram | |
|---|---|
| *(type anything)* | send it into that group's session |
| `/options` | every model and effort level the runtime accepts |
| `/model`, `/effort` | what answers, and how hard it thinks |
| `/to` | hand a message to another seat by name |
| `/md` | *(configurable)* have the agent write its answer to a file and pass the path |
| `/commit` | commit this branch's work, with a message to approve — and push |
| `/review_and_commit` | the same, plus this project's own checks and its review round |
| `/command` | run one of the project's own commands, and hear how it went |
| `/label` | put a label on the task this branch is for |
| `/open` | start an agent that is not running — `claude`, `codex`, `gemini` |
| `/status` | what each seat is, and what is running |
| `/pause`, `/resume` | step out of the way, and come back |

`/md` is one of the `prompts:` in `halyard.yaml` — a sentence you say often
enough to want a name for. Each key there becomes a command. It exists because
a phone is bad at moving text: a long answer arrives split into three messages,
and handing it on means copying each piece while the agent receiving them
starts working on a third of the instruction. Better not to move it at all.

**Files the agent writes** reach you the same way. A turn you start from the
phone runs headless, where Claude Code cannot open its own permission dialog —
so an unlisted `Write` used to be denied outright, with nothing to answer and no
sign of why. Now it asks. Name the places that should not have to ask in
`halyard.yaml`:

```yaml
writes:
  - NOTES/**
```

That is the one thing Halyard permits without a person, so it is empty by
default, every grant is written to the audit log with the pattern that allowed
it, and patterns are matched *inside the project the write belongs to* — a path
that climbs out with `..` or through a symlink is refused however it is spelled.

**MCP calls and web fetches** are gated the same way, and for the same reason:
at the desk they are a popup, and from a phone they were denied with nothing to
answer. An MCP server is mostly read-only queries that one turn calls dozens of
times, so name those in `halyard.yaml` rather than approving each:

```yaml
tools:
  - mcp__*__list_*
  - mcp__*__get_*
```

One pattern covers the same tool on a local server and a production one. `Bash`
and the file tools cannot be granted here — the first is what the gate is for,
and the second is granted by destination under `writes:`.

**The last four commands are per-project**, and each is one line under the
project in `halyard.yaml`:

```yaml
projects:
  alpha-engine:
    path: ~/code/alpha-engine
    validate: make test-fast          # /commit runs this first, every time
    commands:                         # what /command offers, by name
      test-all: make test-all
      bootstrap: make bootstrap-up
    labels: [andon, rework]           # narrows /label; empty means all of them
    warn_if: [task-id-missing]        # the default; [] turns the warnings off
    confirmation:                     # the extra round, when a guard cannot catch it
      inquiry: NOTES/CONFIRMATION_INQUIRY.md
      review: NOTES/CONFIRMATION_REVIEW.md
```

**`confirmation:` buys a round that a guard cannot.** A test proves what it
tests and a file of invariants proves nothing at all — an agent's attention is
finite, and past a point more written rules are noise competing with the work.
So the commit card offers a third answer beside committing and not committing:
send the round. `inquiry` is put in front of the model while it writes the
message and asks it one more thing — is *this* change worth another look? — and
any flag it raises is lifted out of the message and shown above the card.
`review` is the round itself, and goes to the project's navigator when the
button is pressed; nothing is committed, and committing afterwards is a fresh
`/commit` that reads the branch again. Both files belong to the project, because
what is worth asking again is something a team learns about its own failures.

`/commit` takes the whole working tree, has a message written for it in this
repository's own style, and shows what changed rather than only which files —
and stops there, which is what most changes want. `/review_and_commit` is the
same command with everything the project asked for: `validate:` runs, the
warnings apply, and the round is offered. A failing `validate:` means no card at
all. Two commands rather than a list of paths that skip the gate: such a list
has to be maintained against a repository that keeps growing and gets it wrong
quietly, while a command is chosen by somebody who already knows which they
meant. `/command` runs in the background and
reports the tail when it finishes, one at a time per project. `/label` reads
the task number off the branch and asks its issue tracker — `HALYARD_FORGE_TOKEN`
is the only thing it needs, and only a host that cannot name itself needs
`forge:` as well.

When Claude Code asks a **multiple-choice question** (its `AskUserQuestion`
tool), the options arrive on your phone as buttons — tap one, or reply with your
own answer. The choice goes straight back into the session, and the picker at
the desk never appears. Nobody answers in time, or Halyard is paused, and it
falls back to that picker rather than deciding for you. Claude Code only for now.

**When a session is compacted**, half its context goes and what it loses is the
expensive half: which numbers were measured rather than claimed, what you
already rejected and why, the corrections it made to its own earlier mistakes.
What comes back is a confident agent working from a slightly older version of
events.

Two files per seat close that, and neither needs you to be watching:

```yaml
seats:
  nav:
    runtime: claude-code
    before_compaction: compaction_hook/precompact.md
    after_compaction: compaction_hook/postcompact.md
```

`before_compaction` is instructions for a record Halyard writes *about* the
session as it is compacted — a separate one-shot turn reads the transcript, so
the live session is never resumed or forked. `after_compaction` is what the
session is handed once the summary is done. Both are optional and per seat: a
navigator holding a plan needs them, a driver running one command does not.

Measured before it was built: a hook cannot steer the summary itself — output
from `PreCompact` is refused by the runtime as an injection attempt — and
`SessionStart` after a compaction is the one channel that reaches the model.
The whole path is best effort, so a record that is late or a file that is
missing costs orientation, never the session.

| On the machine | |
|---|---|
| `halyard init` | guided setup: `halyard.yaml`, wiring, and a check |
| `halyard doctor` | what is wired, where, and what is broken |
| `halyard verify` | prove the gate stops things, by running into it |
| `halyard wire` / `unwire` | put the gate on a project, or take it off |
| `halyard sessions` | session names this machine can see |
| `halyard service install` | run it as a launchd service (macOS) that updates itself first |

`halyard service install` sets up a launchd agent that comes back after a crash
and after a reboot. Every time it starts it runs `git pull --ff-only`, then
`uv sync`, then serves — so the machine you leave running stays current without
you logging in to update it. The pull is fail-open: it never rewinds or touches
local changes, and a pull it cannot fast-forward is skipped so the last
known-good code still serves. It runs the code it pulls, so point the branch at
a remote you control; `install` prints which one. `uninstall` and `status` do
what they say. macOS only — on Linux, run `halyard serve` under a systemd unit.

## Known limitations

- **The desktop apps show an injected turn late, not never.** A message from your
  phone reaches the session and its reply comes back to you; the app catches up when
  its window is focused again.
- **Two things can outrun the gate.** A hook that exceeds its timeout, and a wrapper
  that cannot start at all, both let the command through. `doctor` checks for the
  second.
- **One bot token per machine.** Telegram's `getUpdates` has a single consumer.
- **`/commit` reads the whole working tree, not the staging area.** Agents write
  code and stage nothing, so a control plane that answered "nothing is staged"
  would be refusing the only thing it was asked for. What `.gitignore` excludes
  is excluded, and the card names the files that are new.
- **A commit runs the project's own check first, when there is one.** `validate:`
  under a project — `make test-fast` — runs on every `/commit`, and a failing
  check means no card at all rather than a question nobody can usefully answer.
- **Agents can be stopped from committing at all.** `HALYARD_REFUSE_AGENT_COMMITS`
  refuses an agent's own `git commit` or `git push` before anybody is asked. Off
  by default. Unlike everything else the gate does, `/pause` does not lift it: a
  pause means "stop asking me", and a guard a pause switches off is a guard
  nobody can rely on.
- **`/pause` steps aside rather than denying.** The runtime's own permission list
  then decides, with no card and no audit entry.
- **The gate covers what the matcher covers** — Bash, the file tools, the
  question tool, MCP calls, `WebFetch` and `WebSearch`. Not `Read`, `Grep` or
  the client's own tool loading, which fire too and would put a card in front of
  every file read. Widening it also widens what stops when Halyard is down:
  those tools are denied then, exactly as Bash is.
- **Multiple-choice questions reach you from a desktop or terminal session, not from a
  turn you started over Telegram.** A Telegram-initiated turn runs headless, where
  Claude Code does not load `AskUserQuestion` — so it asks in prose you can just reply
  to, rather than as buttons.
- **Only macOS has been run.** Nothing here is deliberately macOS-only and the
  Linux paths exist, but they have not been exercised: the CLIs are found by
  looking in places a Mac keeps them, and holding the machine awake uses
  `caffeinate`, which simply does nothing elsewhere. Windows is untried
  entirely. If you run it somewhere else, `halyard doctor` is the first thing
  to read and the place a wrong assumption will show up.
- **Antigravity is not shipped.** The adapter works — it gates, it delivers, it
  brings replies back — but a command approved from your phone is still approved a
  second time in the desktop app whenever the agent asks to leave the sandbox.
  Antigravity ignores a hook that tries to grant that, deliberately: honouring it
  would let whoever controls the hook run unsandboxed code on the machine. That is
  the right call, so the runtime waits rather than shipping with a second prompt.
  Everything found along the way is written down in the notes below.

## What this is not

Out of scope, and not arriving later without a stated reason: remote desktop or
terminal streaming, automatic `allow all`, letting a model decide permissions on your
behalf, uncontrolled agent-to-agent messaging, or multi-user RBAC.

## Documentation

| | |
|---|---|
| [Before you wire it in](docs/before-you-wire-it.md) | What changes, and what surprised us |
| [When it does not work](docs/when-it-does-not-work.md) | Every way setup has gone wrong so far, and the fix |
| [Setup](docs/setup.md) | Installing it, seats in YAML, gating a project by hand |
| [Telegram](docs/telegram.md) | The bot, seats, models and effort |
| [Architecture](docs/architecture.md) | How the layers fit, and the security posture |
| [Hook behaviour](docs/hook-payload-notes.md) | What the runtimes' hooks actually do — measured |
| [Session I/O](docs/session-io-notes.md) | Writing into a live session, and what forks it |
| [Antigravity notes](docs/antigravity-payload-notes.md) | A third runtime, measured in full and not shipped — and why |
| [Design document](docs/mobile-agent-control-plane.md) | The full plan this is built from |

## Development

```bash
uv run pytest
uv run ruff check .
```

## License

[MIT](LICENSE) © alper arabaci
