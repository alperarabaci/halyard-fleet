# Halyard Fleet

[![CI](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml)

> A control plane for orchestrating coding agents remotely. Approve tool calls, steer sessions,
> route work between agents, and hand off state — from any channel, across any agent runtime.

## Read this before you wire it into anything

Halyard takes over your agent's permission prompt. That is the point of it, and it has
consequences that do not announce themselves at the moment they bite: a denied `ls` looks like a
broken tool, a paused gate looks like a closed one, an expired approval looks like a command that
hung. Nobody should discover these afterwards.

**1. A wired project needs the control plane running.** From the moment the hook is in
`settings.local.json`, a Bash command with Halyard down is *denied* — every one of them, `ls`
included — and there is no terminal prompt to approve it with. Wiring is not something you do once
and forget; it is something the project now depends on. Turning it back off is a command, and it
keeps a backup:

```bash
halyard wire ~/code/my-project     # merges into settings.local.json, backs it up first
halyard unwire ~/code/my-project   # removes only Halyard's hooks, leaves the rest alone
```

**2. It is live from the first command.** There is no arming step. Once the control plane is up and
the hook is wired, approvals go to Telegram immediately. `/pause` is what stops that — and pausing
needs the control plane running too, because the pause switch lives inside it.

**3. An approval expires.** `HALYARD_APPROVAL_TIMEOUT_SECONDS` (300s by default) and then it is
*denied*, not left waiting. A phone with notifications muted is the same as answering no.

### Things that surprised us, kept here so they do not surprise you

Every line below was measured rather than read, usually after it caused a problem.

- **`/pause` does not deny anything — it steps aside.** Claude Code then decides exactly as if the
  hook were never installed, which means its own `permissions.allow` list runs matching commands
  with no prompt, no card, and no audit entry. Safe to pause while you are away; worth knowing that
  a long allow list means more goes through than the word "paused" suggests.
- **Hooks are read once, at session start.** Editing settings mid-session changes nothing until you
  restart the session. Script *contents* are re-read every call, so those can change freely.
- **`settings.local.json` is not yours alone.** Claude Code appends every "don't ask again" to a
  `permissions.allow` list in the same file, and it is gitignored. Never write that file wholesale —
  use `halyard wire`, which merges.
- **The gate covers what the matcher covers.** With `"matcher": "Bash"`, `Write` and `Edit` are not
  gated at all.
- **The project root is the git root.** A session opened in a subdirectory is gated by the `.claude/`
  at the top of its repository — and by nothing, if there is no repository above it.
- **One bot token per machine.** Telegram's `getUpdates` has a single consumer; two control planes
  sharing a token will steal each other's messages.
- **Do not wire the gate into the repository you repair Halyard from.** If the only place you can
  run commands is behind the gate that is stuck shut, the repair is behind it too.
- **Do not type into a session Halyard is writing to.** Two overlapping resumes of one session do
  not fail — they fork silently, and one side's history simply disappears.

Found something in this category that is not on the list? It belongs here, or it belongs fixed.
Open an issue either way.

## The idea

You should not have to operate your computer remotely to stay in control of a coding agent.
Remote desktops, terminal streaming, and mobile IDEs all try to move *the machine* to your phone.
Halyard Fleet moves *the decisions* instead.

When an agent wants to run something consequential, that request is relayed to you over a channel
you already have on your phone. You see what it wants to do, why, and how risky it is — then you
allow or deny. The agent's judgment stays under human control while you are away from the keyboard.

> The user should not operate the computer remotely.
> The user should manage the agent's decisions, direction, state, and coordination.

## Status

Early development. Phase 1 — **Permission Relay** — is in progress.

Phase 1 is deliberately narrow: a single user, a single Claude Code session, and one thing that
works end to end. A real `PreToolUse` permission request is captured, classified, redacted, and
sent to Telegram as an inline-keyboard card. `Allow once` lets the command run. `Deny`, a timeout,
an unreachable control plane, or any error at all stops it.

**The relay fails closed.** Every failure mode — network loss, timeout, a 5xx, a malformed
response — resolves to deny, without exception.

## What this is not

These are out of scope, and will not be added in later phases without a stated reason:

- Remote desktop, terminal screen streaming, or a mobile IDE
- Automatic `allow all`, or letting an LLM decide permissions on your behalf
- Uncontrolled agent-to-agent messaging
- Multi-user RBAC
- More than one agent adapter under development at a time

## Documentation

| | |
|---|---|
| [Setup](docs/setup.md) | Installing it, and putting the gate on a project |
| [Telegram](docs/telegram.md) | The bot, navigator/driver seats, models and effort |
| [Architecture](docs/architecture.md) | How the layers fit, and the security posture |
| [Hook behaviour](docs/hook-payload-notes.md) | What Claude Code's hooks actually do — measured |
| [Session I/O](docs/session-io-notes.md) | Writing into a live session, and what forks it |
| [Design document](docs/mobile-agent-control-plane.md) | The full plan this is built from |

## Quick start

```bash
cp .env.example .env         # then set HALYARD_CHANNEL and the Telegram values
uv sync --extra dev
uv run halyard               # keep this running
uv run halyard wire .        # gate this project (merges; keeps a backup)
uv run halyard doctor        # check what is actually wired, and where
```

`halyard unwire .` hands the project back. Both keep a timestamped copy of the
settings file they touch — see [Setup](docs/setup.md) for why that matters.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## License

MIT
