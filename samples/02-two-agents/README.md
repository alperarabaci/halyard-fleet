# 02 — Two agents, two runtimes

Two small products, each its own repository, and an agent on each: Claude Code on
one, Codex on the other. Each agent speaks in a group of its own, so the group you
type in decides who you are talking to — the same `/sample_02` sent in both sends
each agent its own task. Each project also has a command of its own, `check`, that
you run from your phone with `/command`.

## What is here

| File | What it is |
|---|---|
| `greeting-page/` | A page that says "Hello, world". Its `TASK.md`: a field to type a name into, that the greeting answers to |
| `todo-list/` | A to-do list you can add to. Its `TASK.md`: a "Clear" button |
| `*/check.sh` | Each page's check: says which parts are there, fails if one is missing |
| `PROMPT.md` | The task both agents are sent, as text: read `TASK.md`, do it, run the check |
| `halyard.yaml.example` | What this sample adds to your `halyard.yaml`: the two projects, their `check` command, and the task as the prompt `/sample_02` |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Both CLIs** on this machine: `claude` for Claude Code and `codex` for Codex.
- **Sample 01 cleaned up,** if you ran it — [its last step](../01-one-agent/README.md#clean-up).
  Its agent speaks in your default group, and so does this sample's Claude Code
  agent: a message typed in a group two agents share reaches only one of them, so
  step 3 refuses a group another agent already has.

## Run it

**1. Copy the sample out of the Halyard checkout, and make each product a
repository of its own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/02-two-agents ~/halyard-samples/two-agents
```

```bash
cd ~/halyard-samples/two-agents/greeting-page && git init && git add . && git commit -m "Start from the sample"
```

```bash
cd ~/halyard-samples/two-agents/todo-list && git init && git add . && git commit -m "Start from the sample"
```

Two repositories, because they are two products: each is wired, committed and
checked on its own.

**2. Make a second group, for the Codex agent.** Create a Telegram group, add your
bot to it, and send `/status` there. Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add both projects to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 02 ~/halyard-samples/two-agents
```

It asks which group each agent speaks in. For `greeting-dev`, the Claude Code agent,
Enter keeps your default group (`TELEGRAM_CHAT_ID`); for `todo-dev`, the Codex
agent, paste the id from step 2. Then it adds `greeting-page` and `todo-list`, each
at its folder in the copy, and the prompt `/sample_02`. Your file is copied first, to
`halyard.yaml.<date-time>.bak`, and only added to, as in sample 01.

**4. Gate both, and check them** — from the Halyard checkout:

```bash
uv run halyard wire greeting-page
```

```bash
uv run halyard wire todo-list
```

Codex runs a hook only once you have trusted it, and skips one you have not without
a word — so until then, `todo-list` has no gate at all. `wire` says how: open Codex
in the project and choose **Review hooks** when it asks at startup.

```bash
cd ~/halyard-samples/two-agents/todo-list && codex
```

```bash
uv run halyard doctor
```

Then restart Halyard, so it reads the two projects: `make restart` if it runs as a
service, or stop `uv run halyard` and start it again.

**5. Open the two agents,** each in its own project, with the session named as in
`session:`:

- Claude Code in `~/halyard-samples/two-agents/greeting-page`, named `greeting-page`;
- Codex in `~/halyard-samples/two-agents/todo-list` — the one step 4 opened — with
  its thread named `todo-list`.

`uv run halyard sessions` lists the names Halyard can see, if you would rather put
your sessions' own names in the YAML.

**6. From your phone,** in the Codex agent's group:

- `/command`, then `check` — Halyard runs `./check.sh` in `todo-list` and says how it
  went: it fails, `MISSING #clear`. Halyard runs it itself, so there is no card.
- `/sample_02` — the Codex agent reads `todo-list/TASK.md` and adds the button.
- `/command check` once it has answered: it passes now.

Then the same in your default group, where the Claude Code agent adds the field to
the greeting page.

## What you will see

- Each agent's cards and answers in its own group, never in the other's.
- The same card whichever runtime asks, with the project it comes from.
- `check` failing before the task and passing after it — run by Halyard, not by an
  agent.
- `/status` in either group listing both agents, and saying whose group it is.

## Then

- `/stop` in one group — ends what is running there: a `check`, or that agent's turn
  when your phone sent it. The other group's agent carries on.
- `/pause` in either group — Halyard steps aside for both: there is one gate per
  machine, not one per agent. `/resume` brings the cards back.
- `/commit` in one group — a message for that project's change only, on a card,
  before anything is committed. As in sample 01, there is no remote to push to.

## Clean up

```bash
uv run halyard unwire greeting-page
```

```bash
uv run halyard unwire todo-list
```

Then take the two projects, and `/sample_02` under `prompts:`, out of your
`halyard.yaml`. A wired project needs Halyard running: with it down, the agent's
commands in that project are denied.
