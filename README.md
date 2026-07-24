# Halyard Fleet

[![CI](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Halyard Fleet puts your coding agent's permission prompt on your phone.

When Claude Code or Codex wants to run something, you see the command, the project it
came from, and how risky it is — then you allow or deny from Telegram. You can also
send new instructions into the running session and read its replies there.

It runs on your own machine. No open ports, no exposed API, nothing to log into.

Nothing is ever approved automatically: every failure — a crash, a timeout, an
unreachable control plane — denies.

**Runtimes:** Claude Code, Codex &nbsp;·&nbsp; **Channel:** Telegram

<!-- Demo goes here:
<p align="center">
  <img src="assets/demo.gif" width="70%" alt="Approving a command from Telegram" />
</p>
-->

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
uv run halyard init      # asks what you have, writes .env, wires the project, checks it
uv run halyard           # keep this running
```

`init` asks how many Claude and Codex seats you have, offers the session names it can
already see, and reads the bot token without echoing it. It backs up any `.env` it
replaces and keeps settings it does not manage.

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
| `/status` | what each seat is, and what is running |
| `/pause`, `/resume` | step out of the way, and come back |

| On the machine | |
|---|---|
| `halyard init` | guided setup: `.env`, wiring, and a check |
| `halyard doctor` | what is wired, where, and what is broken |
| `halyard verify` | prove the gate stops things, by running into it |
| `halyard wire` / `unwire` | put the gate on a project, or take it off |
| `halyard sessions` | session names this machine can see |

## Known limitations

- **The desktop apps show an injected turn late, not never.** A message from your
  phone reaches the session and its reply comes back to you; the app catches up when
  its window is focused again.
- **Two things can outrun the gate.** A hook that exceeds its timeout, and a wrapper
  that cannot start at all, both let the command through. `doctor` checks for the
  second.
- **One bot token per machine.** Telegram's `getUpdates` has a single consumer.
- **`/pause` steps aside rather than denying.** The runtime's own permission list
  then decides, with no card and no audit entry.
- **The gate covers what the matcher covers** — Bash today, not `Write` or `Edit`.

## What this is not

Out of scope, and not arriving later without a stated reason: remote desktop or
terminal streaming, automatic `allow all`, letting a model decide permissions on your
behalf, uncontrolled agent-to-agent messaging, or multi-user RBAC.

## Documentation

| | |
|---|---|
| [Before you wire it in](docs/before-you-wire-it.md) | What changes, and what surprised us |
| [Setup](docs/setup.md) | Installing it, and gating a project by hand |
| [Telegram](docs/telegram.md) | The bot, seats, models and effort |
| [Architecture](docs/architecture.md) | How the layers fit, and the security posture |
| [Hook behaviour](docs/hook-payload-notes.md) | What the runtimes' hooks actually do — measured |
| [Session I/O](docs/session-io-notes.md) | Writing into a live session, and what forks it |
| [Design document](docs/mobile-agent-control-plane.md) | The full plan this is built from |
| [Postmortems](docs/postmortem/) | Regressions, and what they cost |

## Development

```bash
uv run pytest
uv run ruff check .
```

## License

[MIT](LICENSE) © alper arabaci
