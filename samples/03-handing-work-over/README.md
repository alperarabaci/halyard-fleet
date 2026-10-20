# 03 — Handing work over

One project and two agents on it: a **navigator** who plans and checks, and a
**driver** who makes the change. Each speaks in a group of its own. Work moves
between them from your phone, three ways:

- `/forward` — the last reply in this group, to the agent you pick;
- `/to <agent> <text>` — a message of your own, to an agent by name, from any group;
- `/transition` — the last reply to its next stage, the way the project defines it:
  here `report`, which takes the driver's report to the navigator with the project's
  own short prompt in front.

Whichever way it goes, the agent answers in its own group, so each conversation
reads from top to bottom in one place.

## What is here

| File | What it is |
|---|---|
| `index.html` | A reading list: five books, nothing else |
| `TASK.md` | The task: a field that filters the list, and a count of what is shown |
| `check.sh` | The page's check: says which parts are there, fails if one is missing |
| `transitions/report.md` | The `report` transition's prompt, put in front of the driver's report |
| `PROMPT.md` | What the navigator is asked, as text: a plan for the driver, changing nothing |
| `halyard.yaml.example` | What this sample adds to your `halyard.yaml`: the project, its two agents, the `report` transition, and the prompt `/sample_03` |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the navigator speaks in your
  default group, and step 3 refuses a group another agent already has.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its
own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/03-handing-work-over ~/halyard-samples/reading-list
```

```bash
cd ~/halyard-samples/reading-list && git init && git add . && git commit -m "Start from the sample"
```

**2. Make a second group, for the driver.** Create a Telegram group, add your bot to
it, and send `/status` there. Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 03 ~/halyard-samples/reading-list
```

It asks which group each agent speaks in. For `reading-nav`, the navigator, Enter
keeps your default group (`TELEGRAM_CHAT_ID`); for `reading-drv`, the driver, paste
the id from step 2. Then it adds the project, its `report` transition and the prompt
`/sample_03`. Your file is copied first, to `halyard.yaml.<date-time>.bak`, and only
added to, as in sample 01.

**4. Gate it, and check it** — from the Halyard checkout:

```bash
uv run halyard wire
```

With no project named, `wire` wires every project in your `halyard.yaml`: this one,
and any of yours, which it leaves as they are once wired.

```bash
uv run halyard doctor
```

Then restart Halyard, so it reads the new project: `make restart` if it runs as a
service, or stop `uv run halyard` and start it again.

**5. Open the two agents,** both Claude Code, both in `~/halyard-samples/reading-list`:
one session named `reading-list-nav`, the other `reading-list-drv`. `uv run halyard
sessions` lists the names Halyard can see, if you would rather put your sessions' own
names in the YAML.

**6. From your phone:**

- In the navigator's group, `/sample_03` — the navigator reads `TASK.md` and writes a
  plan for the driver. It changes nothing.
- In the same group, `/forward`, then the `reading-drv` button — the plan goes to the
  driver. The driver's cards, and its answer, arrive in the driver's group.
- Once the driver has answered, in the driver's group: `/transition`, then `report`
  — its report goes to the navigator with `transitions/report.md` in front. The
  navigator checks the report against the change, and answers in its own group.
- If the navigator says something is missing, hand that back: `/forward` in its
  group, then `reading-drv`. Or say it yourself, from any group:
  `/to reading-drv Show the count when the field is empty too.`

## What you will see

- Each agent's cards and answers in its own group, whichever group you typed in.
- `/forward` asking which agent, a button each, with the start of the reply it is
  about to hand on.
- `🤝 report → reading-nav (navigator)` in the driver's group when the transition
  goes, and the navigator's answer in the navigator's group.
- `index.html` filtering as you type, when you open it in a browser.

## Then

- `/status` in either group — both agents, and whose group it is.
- `/to reading-nav <a question>` in the driver's group — the navigator answers in
  its own group, not the one you typed in.
- `/commit` in the driver's group — a message for the change, on a card, before
  anything is committed. As in sample 01, there is no remote to push to.

## Clean up

```bash
uv run halyard unwire reading-list
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_03` under `prompts:`, out of
your `halyard.yaml`. A wired project needs Halyard running: with it down, the
agent's commands in that project are denied.
