# 01 — One agent

The simplest way to use Halyard: one person, one agent, one task. You send the task
from your phone with one command, every command the agent wants to run reaches you
as a card to allow or deny, and its answer arrives in the same chat.

The project is a single HTML page, a counter with one button. The task is to add a
second button and run the page's check.

## What is here

| File | What it is |
|---|---|
| [`index.html`](index.html) | The page: a counter and an "Add one" button |
| [`check.sh`](check.sh) | The page's check: says which buttons are there, fails if one is missing |
| [`PROMPT.md`](PROMPT.md) | The task, as text |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, and the task as the prompt `/sample_01` |

## Before you start

- **Halyard installed, with a Telegram bot in a group** — the
  [Quick start](../../README.md#quick-start). Your `halyard.yaml` needs your own bot
  token, group id and user id: values left as an example's are what
  `uv run halyard doctor` fails on.
- **The CLI of the agent you use**, on this machine: `claude` for Claude Code, `codex`
  for Codex, or `opencode`. Halyard sends your messages through it.
- **Claude Code only:** a long-lived token, so messages from the phone keep arriving
  after the desktop login expires —
  [how to make one and where it goes](../../docs/when-it-does-not-work.md#deliveries-stop-with-oauth-session-expired-and-could-not-be-refreshed).
  `uv run halyard doctor` warns while it is not set.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/01-one-agent ~/halyard-samples/hello-counter
```

```bash
cd ~/halyard-samples/hello-counter && git init && git add . && git commit -m "Start from the sample"
```

Out of the checkout because `halyard wire` gates the repository a folder is in: left
where it is, it would gate Halyard's own repository instead.

**2. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 01 ~/halyard-samples/hello-counter
```

It asks which group the agent speaks in — Enter keeps your default group
(`TELEGRAM_CHAT_ID`) — and adds the project with the path you give it, and the prompt
`/sample_01`. A group another agent already speaks in is refused, since a message
typed there would reach only one of them; `/status` in a group says its id and whose
it is. Your file is copied first, to `halyard.yaml.<date-time>.bak`, and only added
to: your settings, your other projects, your prompts and your comments stay as they
were. With no `halyard.yaml`
yet, it writes one for you to fill in — or run `uv run halyard init`. What it adds
comes from [`halyard.yaml.example`](halyard.yaml.example), where each part has a
comment saying what it is for: open it to see the whole setup before you add it, or
to take its `projects:` and `prompts:` blocks by hand.

**3. Gate it, check it, and start Halyard** — from the Halyard checkout:

```bash
uv run halyard wire
```

```bash
uv run halyard doctor
```

```bash
uv run halyard
```

With no project named, `wire` wires every project in your `halyard.yaml` — this one,
and any of yours, which it leaves as they are once wired. If Halyard already runs as
a service, restart it instead of the last command, so it reads the new project.

**4. Open the agent in `~/halyard-samples/hello-counter`,** and give the session the
name in `session:`: `hello-counter`. `uv run halyard sessions` lists the names Halyard
can see, if you would rather put your session's own name in the YAML.

**5. Send the task from your phone:** `/prompts` in the group, then the `/sample_01`
button — or type `/sample_01`. With `TELEGRAM_MENU: simple` in your settings the bot
shows a short command menu, which is all this sample needs; `/help` lists every
command.

## What you will see

- The task arriving in the agent's session.
- A card on your phone for each command it wants to run — `./check.sh` among them —
  with the command, the project and how risky it looks. Allow it, or deny it and
  watch the agent change course.
- The agent's answer in the group: what `./check.sh` printed.
- `index.html` with a "Reset" button, when you open it in a browser.

## Then

- `/status` — what the agent is, and whether it is working.
- `/pause` — Halyard steps aside and the agent asks at the desk; `/resume` brings the
  cards back.
- `/commit` — Halyard writes a message for the change and shows it on a card before
  anything is committed. It warns that the branch names no task, which is fine here.
  This sample has no remote, so committing and pushing would make the commit and say
  the push had nowhere to go.

## Clean up

```bash
uv run halyard unwire hello-counter
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_01` under `prompts:`, out of
your `halyard.yaml`. A wired project needs Halyard running: with it down, the
agent's commands in that project are denied.
