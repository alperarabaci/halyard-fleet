# Architecture

How the pieces fit, what each layer is allowed to know, and the security
properties that follow from the shape.

## The shape

```
Claude Code (PreToolUse hook, blocking)
    │  stdin: JSON payload
    ▼
hook_bridge.py                          # standalone, minimal, stdlib only
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
    │  stdout: translated into the hook output format
    ▼
Claude Code continues, or stops
```

Two separations carry the design:

**Channels and agents are different things.** Telegram is a channel. Claude Code is an agent
runtime. Both will be replaced. `core/` must know about neither — it speaks only in `AgentEvent`,
`ApprovalRequest`, and the two adapter protocols.

**The bridge is deliberately stupid.** `hook_bridge.py` reads stdin, makes one HTTP call, and
translates the reply into the hook output format — roughly 60 lines of stdlib, no dependencies.
All policy, risk classification, redaction, and routing live in core, where they can be tested.

## Layout

```
halyard-fleet/
├── docs/
│   ├── setup.md                        # installing it, wiring a project
│   ├── telegram.md                     # the bot, seats, models
│   ├── architecture.md                 # this file
│   ├── mobile-agent-control-plane.md   # full design document
│   ├── hook-payload-notes.md           # observed Claude Code hook behaviour
│   └── session-io-notes.md             # writing into a live session
├── src/halyard/
│   ├── core/          # events, registry, approvals, policy, audit, redaction
│   ├── channels/      # ChannelAdapter protocol + Telegram
│   ├── agents/        # AgentAdapter protocol + Claude Code
│   ├── api/           # FastAPI app and routers
│   ├── doctor.py      # what is wired, where, and whether it works
│   ├── wiring.py      # halyard wire / unwire
│   └── config.py
├── bridge/            # runs as a Claude Code subprocess, stdlib only
│   ├── hook.sh        # the wrapper: denies whatever the Python cannot answer
│   ├── hook_bridge.py # the approval bridge
│   ├── relay.py       # the Stop-hook relay, which fails open on purpose
│   └── _settings.py   # config lookup with no dependency on the package
└── tests/
```

## Security posture

- The service binds to `127.0.0.1` by default and never exposes itself on a public interface.
  For remote access, put it behind Tailscale or WireGuard.
- Every approval request carries a single-use nonce and an expiry. A second press of the same
  button is rejected as already resolved.
- Only the Telegram user IDs listed in `.env` can resolve an approval. Callbacks from anyone else
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
