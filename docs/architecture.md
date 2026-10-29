# Architecture

How the pieces fit, what each layer is allowed to know, and the security
properties that follow from the shape.

## The shape

```
Claude Code, Codex, ZCode (PreToolUse hook, blocking)
    │  stdin: JSON payload               # opencode: its plugin makes the same call
    ▼
hook.sh → hook_bridge.py                # standalone, stdlib only
    │  POST /v1/approvals  (blocking, with timeout)
    ▼
Halyard Core (FastAPI)
    ├── redaction  → mask secrets
    ├── policy     → classify risk (never trust the agent's own claim)
    ├── approvals  → issue requestId + nonce + expiry, create a Future
    ├── audit      → record the request
    └── channel.send_approval_request(...)
              ▼
        Telegram Bot API  →  the user
              ▼
        callback_query: "hf:<requestId>:<nonce>:<action>"
              ▼
    ├── verify the nonce, mark it used
    ├── audit      → record the decision
    └── Future.set_result(decision)
    ▲
    │  200 OK: {"decision":"allow"|"deny","reason":"..."}
hook_bridge.py
    │  stdout: translated into the runtime's hook output format
    ▼
the agent continues, or stops
```

Two separations carry the design:

**Channels and agents are different things.** Telegram is a channel. Claude Code, Codex, opencode
and ZCode are agent runtimes, and more of either can come. `core/` must know about neither — it
speaks only in `AgentEvent`, `ApprovalRequest`, and the two adapter protocols.

**The bridge is deliberately stupid.** `hook_bridge.py` reads stdin, makes one HTTP call, and
translates the reply into the runtime's hook output format — stdlib only, no dependencies. It has
grown to speak each runtime's payload, but it decides nothing: all policy, risk classification,
redaction, and routing live in core, where they can be tested.

## Layout

```
halyard-fleet/
├── halyard.simple.yaml.example   # one project, one agent, one chat
├── halyard.yaml.example          # everything a machine's halyard.yaml can say
├── Makefile                      # wire, restart, doctor, runs-advice… as make targets
├── docs/
│   ├── setup.md                  # installing it, the runtimes, wiring a project
│   ├── before-you-wire-it.md     # what changes, and what surprised us
│   ├── when-it-does-not-work.md  # every way setup has gone wrong, and the fix
│   ├── commands.md               # every command, on the phone and on the machine
│   ├── configuration.md          # what goes through without a card; a project's own setup
│   ├── transitions.md            # inspections, and the work taken to its next stage
│   ├── workflows.md              # transitions in order, each reply deciding the next
│   ├── telegram.md               # the bot, its groups, models and effort
│   ├── upkeep.md                 # jobs Halyard does about itself
│   ├── limitations.md            # what it does not do, or not well yet
│   ├── architecture.md           # this file
│   ├── *-notes.md, codex-*.md    # what the runtimes' hooks and sessions do, measured
│   ├── design/                   # an adapter designed before it was built
│   └── postmortem/               # what went wrong, and what changed because of it
├── samples/                      # ten small projects, from one agent to a team
├── src/halyard/
│   ├── core/          # the path a request takes: events, seats, approvals, policy,
│   │                  # grants, audit, redaction — and the usage it leaves behind
│   ├── agents/        # the runtime protocol; Claude Code, Codex, opencode, ZCode,
│   │                  # and Antigravity, built and not shipped
│   ├── channels/      # the channel protocol, a stub for tests, and Telegram
│   ├── api/           # the FastAPI app the bridges call
│   ├── commands/      # a project's own commands: /command, and a transition's
│   ├── commits/       # /commit: the message, the warnings, the round, what an option reads
│   ├── tasks/         # /label: the task a branch is for, and its issue tracker
│   ├── inspections/   # a project's own inspections: the turn, kept runs, repeats
│   ├── transitions/   # the work taken to its next stage: prompt, commands, inspections
│   ├── workflows/     # transitions in order: decisions, runs, and their journal
│   ├── upkeep/        # jobs about Halyard itself: runs-advice and its prompt
│   ├── applications/  # /open: the apps on this machine
│   ├── doctor.py      # what is wired, where, and whether it works
│   ├── verify.py      # prove the gate is there, by running into it
│   ├── wiring.py      # halyard wire / unwire
│   ├── service.py     # the launchd service that updates itself before it starts
│   ├── *_cli.py       # init, samples, export, inspect, rules, runs, upkeep
│   └── config.py      # the settings, read once at startup from halyard.yaml
├── bridge/            # what the runtimes run: stdlib-only Python, one TypeScript plugin
│   ├── hook.sh            # the wrapper: denies whatever the Python cannot answer
│   ├── hook_bridge.py     # the approval bridge
│   ├── permission_hook.sh # Codex's PermissionRequest, which answers in its own shape
│   ├── relay.py           # the Stop-hook relay, which fails open on purpose
│   ├── compaction.py      # hands a session its record back after a compaction
│   ├── opencode.ts        # the gate, as an opencode plugin
│   ├── inject.py          # Antigravity: what was said while it was not listening
│   ├── observe.sh         # an optional payload observer, not the gate
│   └── _settings.py       # config lookup with no dependency on the package
├── scripts/           # applying a hook change across projects, and rolling it back
└── tests/
```

## Security posture

- The service binds to `127.0.0.1` by default and never exposes itself on a public interface.
  For remote access, put it behind Tailscale or WireGuard.
- Every approval request carries a single-use nonce and an expiry. A second press of the same
  button is rejected as already resolved.
- Only the Telegram user IDs listed in `halyard.yaml` can resolve an approval. Callbacks from anyone else
  are recorded as `unauthorized_callback` and silently ignored.
- Secrets are masked before anything reaches the channel layer. The unmasked command is never
  stored — not in the database, not in the audit log.
- The audit log is append-only. Nothing is ever updated or deleted.


## The timeouts have to stay in order

```
approval deadline  <  bridge HTTP timeout  <  hook timeout
      300s                    330s                600s
```

A hook that exceeds its timeout fails open — Claude Code discards it and runs the command. Every
layer therefore has to answer before the one above it gives up. `hook.timeout` in `settings.json`
must match `HALYARD_HOOK_TIMEOUT_SECONDS`; the service refuses to start if the three are out of
order. Getting this wrong looks like nothing at all: approvals work, denials work, and only an
unanswered request behaves differently — it runs instead of being denied.


---

[← Back to the README](../README.md)
