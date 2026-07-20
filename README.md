# Halyard Fleet

> A control plane for orchestrating coding agents remotely. Approve tool calls, steer sessions,
> route work between agents, and hand off state — from any channel, across any agent runtime.

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

## Architecture

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
│   ├── mobile-agent-control-plane.md   # full design document
│   └── hook-payload-notes.md           # observed Claude Code hook behavior
├── src/halyard/
│   ├── core/          # events, registry, approvals, policy, audit, redaction
│   ├── channels/      # ChannelAdapter protocol + Telegram
│   ├── agents/        # AgentAdapter protocol + Claude Code
│   ├── api/           # FastAPI app and routers
│   └── config.py
├── bridge/
│   └── hook_bridge.py # Claude Code hook script (single file, standalone)
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

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## License

MIT
