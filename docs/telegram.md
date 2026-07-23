# Telegram, seats, and what answers you

Creating the bot, routing a navigator and a driver to different places, and
choosing which model replies to a message sent from your phone.

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

The model already selected by the resumed session, unless you say otherwise
with `/model`.

On macOS Halyard sends that turn through Claude Desktop's bundled Claude Code
engine by default, and sends no `--model` flag unless an override was chosen.
Both details restore the shape that was measured working: an external resume
into an open Desktop-owned opus session continued on opus and appeared in the
open task.

The earlier conclusion that no model flag meant haiku mixed up two different
commands. A fresh headless `claude -p` did use haiku; a `--resume` into the live
Desktop session inherited that session's opus model. The fresh-prompt
measurement does not define resume behavior.

`/model default` clears Halyard's override and returns model choice to the
resumed session/runtime. If you intentionally want every phone turn to replace
the session choice, configure it explicitly:

```
HALYARD_CLAUDE_DEFAULT_MODEL=sonnet
```

An explicit `HALYARD_CLAUDE_BINARY` similarly overrides the selected
executable. `/status` shows the session model and any Halyard override
separately.

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


---

Not wired up yet? Start with [setup](setup.md). Curious how the layers fit?
See [architecture](architecture.md).

[← Back to the README](../README.md)
