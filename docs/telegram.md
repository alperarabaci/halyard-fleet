# Telegram, agents, and what answers you

Creating the bot, routing a navigator and a driver to different places, and
choosing which model replies to a message sent from your phone.

## Setting up Telegram

Four values, three of which come from Telegram itself. None of them belong in the repository —
they go in the `settings:` block of `halyard.yaml`, which is gitignored.

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

### 4. The command menu

Nothing to do here — this one arrives on its own.

Once the control plane has started, it publishes its command list to Telegram, and typing `/` in
the chat then offers them as a menu. Approvals never needed this. What it changes is that a command
is picked rather than remembered, which on a phone is most of the difference.

The published list is the one the bot actually answers, and it includes every entry under
`prompts:` in `halyard.yaml` — name a prompt there and it appears in the menu with the rest.
`/help` prints the same list.

Publishing it is best-effort. A refusal from Telegram is logged and the control plane starts
anyway, because the menu is the label rather than the thing: the bot answers every command whether
Telegram has been told about it or not. So a command that works when typed but never showed up in
the menu is not a fault to chase — the next start publishes the list again.

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
separately — "at the desk" is what the session itself is on, "from here" is what
a turn sent from the phone would use. A Codex agent also shows how full its usage
windows are; Claude Code publishes no such number, so its agents show none.

### Optional: keep a navigator and a driver apart

Two sessions working one codebase in one chat is a mess to read on a phone. To split them, give
each agent its own conversation.

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
   with `/` unless privacy mode is turned off, and leaving it on is the better default. With
   Halyard running, its answer names the group — `This chat: -1001111111111` — and whose it is.
3. Give each agent its group, and its role, under the project in `halyard.yaml`:

   ```yaml
   projects:
     alpha-engine:
       path: ~/code/alpha-engine
       agents:
         nav:
           runtime: claude-code
           session: alpha-navigator
           role: navigator
           chat: "-1001111111111"
         drv:
           runtime: codex
           session: alpha-driver
           role: driver
           chat: "-1002222222222"
   ```

   Quote the ids: unquoted, YAML reads them as numbers. A chat may be a forum topic instead of a
   group of its own — `"-1001234567890:12"` sends to topic 12 in a shared group. Give each agent a
   group or topic of its own: a message typed in one shared by two reaches only one of them.

An agent with no `chat:` speaks in `TELEGRAM_CHAT_ID`, the way it does with one session.

`TELEGRAM_NAVIGATOR_CHAT_ID` and `TELEGRAM_DRIVER_CHAT_ID`, with a session started as
`HALYARD_ROLE=navigator claude`, are the shape this had before a second runtime existed. They are
read only from the environment, when there is no `halyard.yaml`; with one, its agents are the
whole answer.

### Every card closes

A card is a message with buttons, and every one closes — its buttons go, and a line
says what became of it:

- **when it is pressed:** a picker says what was chosen, before it is done, so a second
  press finds nothing to press;
- **when what it asks ends:** an approval or a question answered, here or at the desk,
  or out of time (*⌛ Expired — denied*); a commit made, cancelled or reworded;
- **at its deadline,** if nothing else closed it first;
- **after a restart:** what the last run of Halyard left open closes as it starts
  (*Closed — Halyard restarted*). A workflow run's card adds that the run is kept: the
  run goes on, and `/workflow` gives it a card again.

A workflow run keeps one card with buttons, its newest — see
[Workflows](workflows.md#stopping-steering-and-asking-where-it-is).

### Use the app, not a browser tab

A browser tab gives you no push notifications, so a card can expire unseen and the command is denied
on the timeout. That is fine at the desk, where you are watching anyway. It defeats the purpose
everywhere else, which is the only place this project is for.

### When an approval cannot be delivered

Connection errors and connection timeouts before a Telegram JSON request is sent
get up to three attempts, with one- and two-second pauses. Delivery uses the
approval's original deadline; retries do not extend it.

Read/write failures and Telegram API refusals are not retried. A message may
already have arrived when its response is lost, so sending it again could put
two cards in the chat. If delivery cannot be confirmed, Halyard denies the
request and names the channel and error type in both the hook response and the
audit record. Submit a new request once the channel is working again.


---

Not wired up yet? Start with [setup](setup.md). Curious how the layers fit?
See [architecture](architecture.md).

[← Back to the README](../README.md)
