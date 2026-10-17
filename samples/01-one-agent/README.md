# 01 — One agent

One coding agent on one tiny project, run from your phone: you send it a task, every
command it wants to run reaches you as a card to allow or deny, and its reply arrives
in the same chat. At the end you commit its work with `/commit`.

The project is a single HTML page, a counter with one button. The agent adds a second
one, and runs the page's check.

## What is here

| File | What it is |
|---|---|
| `index.html` | The page: a counter and an "Add one" button |
| `check.sh` | The page's check: says which buttons are there, fails if one is missing |
| `PROMPT.md` | The task you give the agent |
| `halyard.yaml.example` | What to add to your `halyard.yaml` for this project |

## Before you start

Halyard installed and set up, with a Telegram group the bot is in — the
[Quick start](../../README.md#quick-start) covers both. Claude Code, Codex or opencode
on this machine.

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

**2. Add the project to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 01 ~/halyard-samples/hello-counter
```

It adds this sample's project to the file with the path you give it, and the agent
speaks in your default group (`TELEGRAM_CHAT_ID`). Your file is copied first, to
`halyard.yaml.<date-time>.bak`, and only added to: your settings, your other
projects and your comments stay as they were. With no `halyard.yaml` yet, it writes
one from `halyard.yaml.example` for you to fill in — or run `uv run halyard init`.
By hand instead, take the `projects:` block from `halyard.yaml.example`.

**3. Gate it, check it, and start Halyard** — from the Halyard checkout:

```bash
uv run halyard wire hello-counter
```

```bash
uv run halyard doctor
```

```bash
uv run halyard
```

If Halyard already runs as a service, restart it instead of the last command, so it
reads the new project.

**4. Open the agent in `~/halyard-samples/hello-counter`,** and give the session the
name in `session:` — `hello-counter`. `uv run halyard sessions` lists the names Halyard
can see, if you would rather put your session's own name in the YAML.

**5. Give it the task.** Paste the text of `PROMPT.md` into the session — or type it
into the Telegram group, which sends it into the same session.

## What you will see

- A card on your phone for each command the agent wants to run — `./check.sh` among
  them — with the command, the project and how risky it looks. Allow it, or deny it
  and watch the agent change course.
- The agent's reply, in the group, once it is done: what `./check.sh` printed.
- `index.html` with a "Reset" button, when you open it in a browser.

## Then

- `/commit` in the group: Halyard writes a message for the change and shows it on a
  card before anything is committed. It warns that the branch names no task — this
  sample has none, which is fine. Commit it from the card; this sample has no remote,
  so committing and pushing would make the commit and say the push had nowhere to go.
- `/status` — what the agent is, and whether it is working.
- `/pause` — Halyard steps aside and the agent asks at the desk; `/resume` brings the
  cards back.

## Clean up

```bash
uv run halyard unwire hello-counter
```

Then take the project out of your `halyard.yaml`. A wired project needs Halyard
running: with it down, the agent's commands in that project are denied.
