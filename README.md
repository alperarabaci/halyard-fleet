# Halyard Fleet

[![CI](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/alperarabaci/halyard-fleet/actions/workflows/ci.yml)

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

## Running it

```bash
cp .env.example .env       # then set HALYARD_CHANNEL
uv sync --extra dev
uv run halyard
```

Or without installing Python at all:

```bash
cp .env.example .env       # then set HALYARD_CHANNEL
docker compose up -d
docker compose logs -f
```

**Only the control plane is containerised.** The hook bridge cannot be — it runs inside Claude
Code's process tree on your machine, so it stays on the host and reaches the container over
`HALYARD_URL`:

```
Claude Code ──hook──► bridge/hook.sh ──HTTP──► 127.0.0.1:8787 ──► control-plane container
   (host)              (host)                    (published)         (Telegram, audit log)
```

The published port is bound to `127.0.0.1` on purpose. Writing `8787:8787` instead would put the
control plane on every interface of the host, and anything on the network could then approve
commands on your machine.

> **If port 8787 is already in use, change `HALYARD_BIND`** in `.env` — Docker Desktop itself
> listens on 8787 on some machines. That one key is the whole address: the service binds to it,
> compose publishes to it, and the bridges derive their URL from it. There is nothing else to keep
> in sync.
>
> Check it before you debug anything else, because the symptom points somewhere else entirely.
> When the port cannot be bound, Docker starts the container **without a network** rather than
> refusing to start it, so the logs fill with `Temporary failure in name resolution` and it looks
> like broken DNS. It is not:
>
> ```bash
> docker inspect halyard-control-plane --format '{{json .NetworkSettings.Networks}}'
> # {}  ← no network attached; check the port, not the resolver
> ```
>
> `halyard doctor` walks the same path a hook does and reports which step broke and where each
> setting came from.

The audit log lives in a named volume so it survives rebuilds. To read it:

```bash
docker compose exec control-plane cat /data/audit.jsonl
```

If you would rather open it in an editor, swap the volume for a bind mount — there is a commented
line in `docker-compose.yml` showing how, and a note about the ownership it needs.

Then point Claude Code at the bridge in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/bridge/hook.sh",
            "timeout": 600
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/bridge/relay.py",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

**`PreToolUse` → `hook.sh`** is the approval gate. Point it at the wrapper, not at
`hook_bridge.py`: the wrapper is what denies when the Python process cannot start at all — a
missing interpreter, a bad path, an import error. Those exit non-zero with nothing on stdout, which
Claude Code reads as *no opinion*, and it runs the command.

**`Stop` → `relay.py`** sends the agent's replies to your phone. It is optional; approvals work
without it.

The two have opposite rules, which is why they are separate files. The approval bridge denies on
every error, because something is waiting on its answer. The relay swallows every error and prints
nothing, because a lost chat message is not worth interrupting a session over. Neither should ever
be given the other's behaviour.

### Once the hook is wired, the terminal stops asking

This is the part worth understanding before you wire it up.

A `PreToolUse` hook decides *instead of* Claude Code's own permission prompt, not alongside it. So
from the moment the hook is installed, **the prompt in your terminal no longer appears for the
tools it matches** — the question goes to your phone and the answer comes back from there. Sitting
at the keyboard does not give you a second way to say yes.

The consequence to plan for: if the control plane is not reachable, every matching command is
denied, and there is no terminal fallback to approve it with. That is the fail-closed guarantee
working correctly, and it means the recovery path is fixing the control plane — not clicking
through a prompt.

Two practical habits follow:

- **Do not wire the gate into the repository you fix Halyard from.** If the control plane breaks
  and the only place you can run commands is behind the gate it is holding shut, you have locked
  yourself out of the repair.
- **Keep a Telegram client where notifications actually reach you.** An approval expires after
  `HALYARD_APPROVAL_TIMEOUT_SECONDS` and is then denied. A browser tab you closed is not a client.

**Claude Code snapshots hook configuration at startup.** Editing `settings.json` mid-session has no
effect; restart the session. The script contents are read on every call, so those can change freely.

## Setting up Telegram

Four values, three of which come from Telegram itself. None of them belong in the repository —
they go in `.env`, which is gitignored.

### 1. Create a bot

Message [@BotFather](https://t.me/BotFather) and send `/newbot`. It asks for a display name and a
username ending in `bot`. It replies with a token that looks like `8683402306:AAH…`.

```bash
TELEGRAM_BOT_TOKEN=<the token>
```

Treat it like a password. Anyone holding it can read every approval card this bot sends — the
commands, which project they came from, when you were away. They cannot *approve* anything, because
that check is on the user id rather than the bot, but reading is bad enough. If it ever leaks, send
BotFather `/revoke`, pick the bot **from the buttons it offers** (typing the name gives
`Invalid bot selected`), and it hands you a new one immediately.

### 2. Find your own user id

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric id.

```bash
TELEGRAM_AUTHORIZED_USER_IDS=<your id>
```

This is the list of people who may decide. A callback from anyone else is recorded as
`unauthorized_callback` and ignored without a reply. Comma-separate for more than one.

### 3. Find the chat id

Send your new bot any message, then ask Telegram what it received:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
```

Take `message.chat.id` from the response. A private chat gives a positive number; a group gives a
negative one, which is normal.

```bash
TELEGRAM_CHAT_ID=<the chat id>
```

For a group, add the bot to it first and send a message there instead — group chats are what make
it possible to keep separate conversations later, since a bot can only hold one private chat with
you.

### 4. Optional: a command menu

Approvals work without this. It only makes the commands discoverable instead of remembered.

Send BotFather `/setcommands`, choose your bot, and paste:

```
status - What is happening right now
pause - Stop sending here; the terminal asks instead
resume - Start sending here again
```

Typing `/` in the chat then offers them as a menu.

### Use the app, not a browser tab

A browser tab gives you no push notifications, so a card can expire unseen and the command is denied
on the timeout. That is fine at the desk, where you are watching anyway. It defeats the purpose
everywhere else, which is the only place this project is for.

### The timeouts have to stay in order

```
approval deadline  <  bridge HTTP timeout  <  hook timeout
      300s                    330s                600s
```

A hook that exceeds its timeout fails open — Claude Code discards it and runs the command. Every
layer therefore has to answer before the one above it gives up. `hook.timeout` in `settings.json`
must match `HALYARD_HOOK_TIMEOUT_SECONDS`; the service refuses to start if the three are out of
order. Getting this wrong looks like nothing at all: approvals work, denials work, and only an
unanswered request behaves differently — it runs instead of being denied.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## License

MIT
