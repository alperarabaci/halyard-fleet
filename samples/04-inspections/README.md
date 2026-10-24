# 04 — Inspections

Sample 03's navigator and driver, on a packing list, with one thing added: the driver's report is
inspected before the navigator gets it.

An **inspection** is the project's own text, run over a reply as a model turn of its
own: one-shot and read-only. It reads the files and the diff to see whether the
reply holds up, and changes nothing. A transition that names an inspection runs it
before it delivers, and waits for it: here `report` runs `claims` over the driver's
report, and the navigator gets the report and what `claims` found together.
`/inspect` runs one by hand, over the last reply in the group you type it in.

Each inspection costs a model turn, on `HALYARD_INSPECTION_MODEL`: Sonnet on Claude
Code unless set, and a `gpt-…` model runs on Codex. A read the gate does not pass on
its own reaches your phone as an **INSPECTION** card.

## What is here

| File | What it is |
|---|---|
| `index.html` | A packing list: five things to tick, nothing else |
| `TASK.md` | The task: a checkbox that hides what is packed, and a count of what is left |
| `check.sh` | The page's check: says which parts are there, fails if one is missing |
| `inspections/claims.md` | The `claims` inspection: which of the report's claims the change backs, which it does not, and which could not be measured |
| `transitions/report.md` | The `report` transition's prompt, put in front of the report and the inspection's answer |
| `PROMPT.md` | What the navigator is asked, as text: a plan for the driver, changing nothing |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, its two agents, the `claims` inspection, the `report` transition that runs it, and the prompt `/sample_04` |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the navigator speaks in your
  default group, and step 3 refuses a group another agent already has.
- **The CLI the inspection model runs on:** `claude` unless your
  `HALYARD_INSPECTION_MODEL` is a `gpt-…` model, `codex` if it is. `uv run halyard
  doctor` fails a model no CLI here can run.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its
own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/04-inspections ~/halyard-samples/packing-list
```

```bash
cd ~/halyard-samples/packing-list && git init && git add . && git commit -m "Start from the sample"
```

A repository, and not only so it can be wired: the inspection reads the driver's
change as `git diff`.

**2. Make a second group, for the driver.** Create a Telegram group, add your bot to
it, and send `/status` there. Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 04 ~/halyard-samples/packing-list
```

It asks which group each agent speaks in. For `packing-nav`, the navigator, Enter
keeps your default group (`TELEGRAM_CHAT_ID`); for `packing-drv`, the driver, paste
the id from step 2. Then it adds the project, its `claims` inspection, its `report`
transition and the prompt `/sample_04`. Your file is copied first, to
`halyard.yaml.<date-time>.bak`, and only added to, as in sample 01. What it adds
comes from [`halyard.yaml.example`](halyard.yaml.example), where each part has a
comment saying what it is for: open it to see the whole setup before you add it, or
to take its `projects:` and `prompts:` blocks by hand.

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

**5. Open the two agents,** both Claude Code, both in `~/halyard-samples/packing-list`:
one session named `packing-list-nav`, the other `packing-list-drv`. `uv run halyard
sessions` lists the names Halyard can see, if you would rather put your sessions' own
names in the YAML.

**6. From your phone:**

- In the navigator's group, `/sample_04` — the navigator reads `TASK.md` and writes a
  plan for the driver. It changes nothing.
- In the same group, `/forward`, then the `packing-drv` button — the plan goes to the
  driver. The driver's cards, and its report, arrive in the driver's group.
- Once the driver has answered, in the driver's group: `/transition`, then `report`
  — `claims` reads the report against `TASK.md` and the change, and the transition
  waits for its answer. Then the report and the answer go to the navigator together,
  with `transitions/report.md` in front, and the navigator answers in its own group.
- In the driver's group again, `/inspect claims` — the same inspection, by hand, over
  the same report. Its answer comes back in that group.

## What you will see

- `🤝 report → packing-nav (navigator), inspecting claims first…` in the driver's
  group when the transition goes, then `claims: answered` once the answer is in — or
  `unmeasured`, with why. An inspection that did not answer is never a clean one.
- An **INSPECTION** card in the navigator's group for any read `claims` wants that
  the gate does not pass on its own. **Deny** refuses that read, and what it was for
  is left unmeasured.
- The navigator's answer in its own group: the report and the inspection weighed
  against the change, and what is missing, if anything.
- After `/inspect claims`: `🔎 claims is reading the last reply here`, then its
  answer as three lists — backed, not backed, not measured — with a button per agent
  to hand it on.
- `index.html` hiding what is packed and counting what is left, when you open it in
  a browser.

## Then

- `/inspect` with no name — a button per inspection the project has; here, one.
- An answer that found a claim not backed: its `packing-drv` button hands it to the
  driver, with the report it was about.
- Another model: `HALYARD_INSPECTION_MODEL` in your `settings:` for every
  inspection, or `claims` written as a mapping with a `model:` of its own. See
  [Inspections and transitions](../../docs/transitions.md#an-inspection-runs-on-the-machines-model-or-its-own).
- `/commit` in the driver's group — a message for the change, on a card, before
  anything is committed. As in sample 01, there is no remote to push to.

## Clean up

```bash
uv run halyard unwire packing-list
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_04` under `prompts:`, out of
your `halyard.yaml`. A wired project needs Halyard running: with it down, the
agent's commands in that project are denied.
