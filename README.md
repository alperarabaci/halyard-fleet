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

### It runs on your machine, not in a container

There was a Docker image for a while, and removing it is the honest correction to a mistake.

The control plane sends messages into a Claude Code session by running the `claude` CLI, and it
reads session names out of `~/.claude/projects`. A container has neither — no binary, no
credentials, no home directory. So a containerised control plane could relay approvals and output
but could never accept a message back, which is half the product, and having two ways to run it
that quietly differ in what they can do is worse than having one.

`halyard doctor` reports `can_send_messages`, so if this ever regresses it says so rather than
failing at the moment you need it.

### Wiring the hooks

Point Claude Code at the bridges in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/halyard-fleet/bridge/hook.sh",
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
            "command": "/absolute/path/to/halyard-fleet/bridge/relay.py",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

Use an absolute path when the project being gated is not Halyard itself — `$CLAUDE_PROJECT_DIR`
points at whichever repository the session is in. Put it in `.claude/settings.local.json`, which is
gitignored, so a machine-specific path does not break a teammate's checkout.

> **Merge it into that file. Do not replace the file.**
>
> `settings.local.json` is not yours — Claude Code writes to it too. Every time you answer a
> permission prompt with "don't ask again", the rule is appended to a `permissions.allow` list
> that lives in exactly this file. Pasting the block above over the top deletes that list, and
> nothing announces it: approvals still work, the hook still fires, and the only symptom is that
> the session starts asking about commands it stopped asking about months ago.
>
> It is also gitignored, so there is no history to recover it from. Copy the file somewhere first.
>
> ```bash
> cp .claude/settings.local.json .claude/settings.local.json.bak
> ```
>
> The result should have both keys side by side:
>
> ```json
> {
>   "hooks": { "PreToolUse": [ ... ], "Stop": [ ... ] },
>   "permissions": { "allow": [ "Bash(uv run *)", "..." ] }
> }
> ```

**Claude Code snapshots hook configuration at startup.** Editing settings mid-session has no
effect; restart the session. Script contents are read on every call, so those can change freely.

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

**`/pause` is not that.** Pausing does not deny anything; it takes Halyard out of the loop. The
hook returns no opinion, and Claude Code then decides exactly as it would if the hook had never
been installed — which means its own `permissions.allow` list decides. Anything that list covers
runs with no prompt, no card, and no audit entry, and everything else it asks you at the desk.

Both halves of that are worth knowing. It is why pausing from your phone is safe to do while you
are away from the machine, and it is why a long `permissions.allow` list means pausing lets more
through than you might picture.

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
chat - Send a message into the session
options - Every model and effort level you can pick
model - What answers, for turns sent from here
effort - How hard it thinks
status - What is happening right now
pause - Step out of the way; Claude Code decides on its own
resume - Start sending here again
```

Typing `/` in the chat then offers them as a menu.

`/options` is the one to remember, because it lists the rest:

```
claude-code

/model  opus sonnet haiku fable
  ↳ anything else is passed through and may work.

/effort  low medium high xhigh max

Add default to give a choice back to the session.
```

Each runtime answers that question for itself, so a runtime added later appears
there without anything here being edited. The model list is a suggestion rather
than a gate — a name Halyard has never heard of is still handed to the CLI,
because a list written months ago has no business refusing a model that shipped
this morning. Effort is checked, because the CLI documents a closed set and a
typo would otherwise cost a whole turn to discover.

When a new model appears and you would like it offered by name, say so without
waiting for a release:

```
HALYARD_CLAUDE_MODELS=opus,sonnet,haiku,fable,whatever-is-new
```

### What a message from a phone runs on

Sonnet, unless you say otherwise:

```
HALYARD_CLAUDE_DEFAULT_MODEL=sonnet
```

This is an opinion, and it is deliberately not the CLI's. `claude -p` with no
`--model` runs on haiku — measured, not read. That is a sensible default for a
one-shot prompt and the wrong one for continuing work on a codebase, and it is
the kind of wrong you do not notice: the turn still answers, plausibly, and
nothing in the reply mentions which model wrote it.

Two things follow that are worth knowing before they surprise you.

**The model shown in the app is not the model answering your phone.** They are
separate settings and nothing here can reach the first one. A session sitting on
opus at the desk still answers a message from Telegram with whatever this
control plane sends. `/status` prints both, which is why it names them
separately.

**`/model default` returns to this setting, not to the session's.** There is no
way to hand the choice back to the app, so "default" means the default here.

Set it empty to pass no `--model` at all and take whatever the CLI does.

### Optional: keep a navigator and a driver apart

Two sessions working one codebase in one chat is a mess to read on a phone. To split them, give
each seat its own conversation.

**One bot is enough.** A bot is an identity, not a conversation — the same way one person is in many
group chats. The limit people run into is that a bot can hold only *one private chat* with you, so
separate conversations means separate **groups**, not separate bots.

```
@your_bot                                    one bot, one token
  ├── group "alpha-engine / navigator"       chat id -1001111111111
  └── group "alpha-engine / driver"          chat id -1002222222222
```

1. Create two groups and add the same bot to both. A group of one is fine.
2. Send `/status` in each. It has to be a command: a bot in a group only sees messages starting
   with `/` unless privacy mode is turned off, and leaving it on is the better default.
3. **Stop the control plane**, then read the chat ids. This matters — `getUpdates` hands each
   update to whoever asks first, so a running poll loop will swallow them before your `curl` sees
   anything:

   ```bash
   # stop the control plane first
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "
   import sys, json
   for u in json.load(sys.stdin)['result']:
       c = (u.get('message') or {}).get('chat') or {}
       if c: print(c.get('id'), '→', c.get('title') or c.get('username'))"
   ```

4. Put them in `.env` and start again:

   ```bash
   TELEGRAM_NAVIGATOR_CHAT_ID=-1001111111111
   TELEGRAM_DRIVER_CHAT_ID=-1002222222222
   ```

   Either one may be a forum topic instead of a group of its own — `-1001234567890:12` sends to
   topic 12 in a shared group.

Then tell each session which seat it is when you launch it. There is no pairing step and no list to
pick from: the environment a session was started with is the answer, and every hook it fires
inherits it.

```bash
HALYARD_ROLE=navigator claude     # one terminal
HALYARD_ROLE=driver    claude     # the other
```

Worth two aliases, since it is every launch:

```bash
alias nav='HALYARD_ROLE=navigator claude'
alias drv='HALYARD_ROLE=driver claude'
```

Leave the two chat ids unset and none of this applies — everything goes to `TELEGRAM_CHAT_ID`, the
way it does with one session.

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
