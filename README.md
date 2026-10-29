<p align="center">
  <img src="assets/halyard-fleet-banner.png" alt="Halyard Fleet: Raise the sails. Ship the code." width="100%">
</p>

# Halyard Fleet

[![CI](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Halyard Fleet runs your coding agents from your phone — from approving one agent's
commands to running a team of them through your project's own workflow.

- **One agent.** When Claude Code, Codex, opencode or ZCode wants to run something, you
  see the command, the project and how risky it is, and allow or deny it from Telegram.
  You send it instructions and read its replies in the same chat, and a turn that dies
  on an overloaded model or a usage limit reaches you too.
- **A team.** A navigator that plans, a driver that builds, a reviewer on another model,
  each in a group of its own. The work goes from one to the next the way your project
  writes it down — its prompt in front, its tests and inspections run on the way — and
  each agent's last line decides whether it goes forward, comes back, or waits for you.

It runs on your own machine: no open ports, nothing to log into. Every failure denies,
and the little that goes through without a card is what you turned on, written to the
audit log with what allowed it.

**Runtimes:** Claude Code, Codex, opencode, ZCode &nbsp;·&nbsp; **Channel:** Telegram &nbsp;·&nbsp; **Tested on:** macOS

<p align="center">
  <img src="assets/demo.gif" width="70%" alt="Approving a command from Telegram" />
</p>

## Why it exists

Remote desktops and mobile IDEs move *the machine* to your phone. Halyard moves *the
decisions* instead.

> The user should not operate the computer remotely.
> The user should manage the agent's decisions, direction, state, and coordination.

You answer what your agents would otherwise be blocked on — whether a plan is right,
whether the work goes back, whether it is done — from wherever you are.

**If a job is not easier with Halyard, Halyard does not do it.**

## From one agent to a team

Each step adds one thing. Its name leads to its page, and its [sample](samples/) shows it
running, on a project small enough to read in a minute.

| Step | What it adds | From your phone | Sample |
|---|---|---|---|
| **[Approve](docs/before-you-wire-it.md)** | The agent's commands, edits and MCP calls as cards | Allow or Deny | [01](samples/01-one-agent/) |
| **[Steer](docs/commands.md#on-your-phone)** | Its replies, your messages; agents on several runtimes, a group each | Type; `/prompts`; `/model`; `/stop` | [01](samples/01-one-agent/), [02](samples/02-two-agents/) |
| **[Run commands](docs/configuration.md#commands-labels-and-commits)** | The project's own tests and scripts, with no agent and no card; commits | `/command`; `/commit` | [05](samples/05-the-whole-mechanism/) |
| **[Fewer cards](docs/configuration.md#what-goes-through-without-a-card)** | What you approve every time, trusted — proposed from your own log | — | [09](samples/09-fewer-cards/) |
| **[Hand over](docs/telegram.md#optional-keep-a-navigator-and-a-driver-apart)** | A navigator that plans and a driver that builds | `/forward`, `/to` | [03](samples/03-handing-work-over/) |
| **[Inspect](docs/transitions.md)** | The project's own review of a reply, on the model you choose | `/inspect` | [04](samples/04-inspections/) |
| **[Transitions](docs/transitions.md)** | The work taken to its next stage, its commands and inspections run first | `/transition` | [06](samples/06-transitions/) |
| **[Workflows](docs/workflows.md)** | Transitions as steps, each reply's last line deciding the next | `/workflow` | [07](samples/07-a-workflow/), [08](samples/08-three-agents/) |
| **[Measure](docs/transitions.md#every-inspection-can-be-kept)** | Every inspection kept, repeated on another model, compared with its cost | — | [10](samples/10-measuring-inspections/) |

### A team, in one file

The shape of [sample 07](samples/07-a-workflow/), with the driver on Codex and a
second inspection on GPT:

```yaml
projects:
  cart:
    path: ~/code/cart
    commands:
      test-fast: python3 -m unittest -q
    inspections:
      test-reliability: {file: inspections/test-reliability.md, model: opus, effort: high}
      over-engineering: {file: inspections/over-engineering.md, model: gpt-6.1-sol, effort: high}
    transitions:
      develop: {prompt: transitions/develop.md, to: driver}
      developed: {prompt: transitions/developed.md, commands: [test-fast], to: navigator}
      close:
        prompt: transitions/close.md
        commands: [test-fast]
        inspect: [test-reliability, over-engineering]
        to: navigator
    workflows:
      steps:
        develop: {agent: cart-drv, rounds: 2}
        developed: {agent: cart-nav, rounds: 2}
        close: {agent: cart-nav, works_on: develop}
      deliver: [develop, developed, close]
    agents:
      cart-nav: {runtime: claude-code, session: cart-nav, role: navigator, chat: "-1001234567890"}
      cart-drv: {runtime: codex, session: cart-drv, role: driver, chat: "-1002234567890"}
```

`/workflow` in the navigator's group, then **deliver** and **develop**: the plan goes to
the driver; its report comes back with the tests run; at **close** the tests run again,
both inspections read the work, and the navigator closes it or sends it back. Each reply
ends on a line like `DECISION: forward` — or `back`, `wait`, and `done` at the last step.
[Workflows](docs/workflows.md) has the rest: rounds, phases, a reviewer's verdict.

## Quick start

1. **A Telegram bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot`, and
   keep the token. Get your user id from [@userinfobot](https://t.me/userinfobot) — only
   ids you list can approve. Add the bot to a group for each agent.
2. **Install** — Python 3.12+ and [uv](https://docs.astral.sh/uv/), on the host, where
   the agent CLIs are:

   ```bash
   git clone https://github.com/alperarabaci/halyard-fleet.git
   cd halyard-fleet
   uv sync --extra dev
   ```

3. **Set it up and run:**

   ```bash
   uv run halyard init      # asks what you have, writes halyard.yaml, wires it, checks it
   uv run halyard           # keep this running
   ```

> **A wired project depends on this process.** With Halyard down, the agent's commands
> in that project are *denied* — opencode's wait on its own screen instead.
> `halyard unwire <path>` hands it back. Read
> [Before you wire it in](docs/before-you-wire-it.md) before you walk away from the
> machine.

`uv run halyard doctor` says what is wired and what is broken; `uv run halyard verify`
proves the gate stops things. By hand instead of `init`: [Setup](docs/setup.md).

## Samples

Tiny projects to run under Halyard as they are, one idea at a time — [all of them](samples/):

1. [One agent](samples/01-one-agent/) — its commands as cards on your phone, its replies, a commit
2. [Two agents, two runtimes](samples/02-two-agents/) — Claude Code and Codex, each in its own chat
3. [Handing work over](samples/03-handing-work-over/) — a navigator and a driver; `/forward`, `/to`
4. [Inspections](samples/04-inspections/) — the work inspected before it is handed on
5. [The whole mechanism](samples/05-the-whole-mechanism/) — commands, trusted runs, task labels
6. [Transitions](samples/06-transitions/) — work handed there and back, the tests run on the way
7. [A workflow](samples/07-a-workflow/) — the same transitions as steps, each reply deciding the next
8. [Three agents](samples/08-three-agents/) — Claude and GPT on one workflow with phases
9. [Fewer cards](samples/09-fewer-cards/) — what to trust, proposed from the cards you answered
10. [Measuring inspections](samples/10-measuring-inspections/) — the same input on another model, and its cost

## Find your way

| You want to | Read |
|---|---|
| Set it up by hand, or see which runtime needs what | [Setup](docs/setup.md) |
| Know what changes before you leave the machine | [Before you wire it in](docs/before-you-wire-it.md) |
| Fix something that is not working | [When it does not work](docs/when-it-does-not-work.md) |
| Look up a command, on the phone or the machine | [Commands](docs/commands.md) |
| Get fewer cards: trusted commands, `writes:`, `tools:` | [Configuring a project](docs/configuration.md) |
| Set up a project's commands, commits, compaction and models | [Configuring a project](docs/configuration.md) |
| Have the work inspected, and hand it on | [Inspections and transitions](docs/transitions.md) |
| Run your agents through a workflow | [Workflows](docs/workflows.md) |
| Let Halyard propose what to trust | [Upkeep](docs/upkeep.md) |
| Set up the bot, its groups, models and effort | [Telegram](docs/telegram.md) |
| Know the limits before you rely on it | [Known limitations](docs/limitations.md) |
| See how it is built, and its security posture | [Architecture](docs/architecture.md) |
| Read what the runtimes actually do, measured | [Hooks](docs/hook-payload-notes.md) · [Session I/O](docs/session-io-notes.md) · [ZCode](docs/zcode-payload-notes.md) · [Antigravity](docs/antigravity-payload-notes.md) |

## What this is not

No remote desktop or terminal streaming, no automatic `allow all`, no model deciding
permissions for you, no uncontrolled agent-to-agent messaging, no multi-user RBAC.

## Development

```bash
uv run pytest
uv run ruff check .
```

## License

[MIT](LICENSE) © alper arabaci
