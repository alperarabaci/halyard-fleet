# 06 — Transitions

Sample 03's navigator and driver, on sample 05's shopping cart, handing work there and
back. Each hand-over is a press: `/transition`, then where the work goes next. On the
way back, Halyard runs the tests itself, so the driver's report reaches the navigator
with the result beside it — a result, not the driver's word for it.

| At the desk | From your phone |
|---|---|
| Copy the navigator's plan into the driver's session, with what to do with it | `/transition`, then **develop** |
| Run the tests, then paste the driver's report and what they printed into the navigator's session | `/transition`, then **developed** |
| Copy what the navigator says is missing back to the driver | `/transition`, then **develop** again |

## What is here

| File | What it is |
|---|---|
| [`cart.py`](cart.py) | The app: a shopping cart's total, in cents, with a discount |
| [`test_cart.py`](test_cart.py) | Its tests. The shipping tests fail until the task is done |
| [`Makefile`](Makefile) | `make test-fast`: the tests |
| [`TASK.md`](TASK.md) | The task: what shipping costs, and when it is free |
| [`PROMPT.md`](PROMPT.md) | What the navigator is asked: a plan for the driver, changing nothing |
| [`transitions/develop.md`](transitions/develop.md) | The `develop` transition's prompt, put in front of the navigator's plan for the driver |
| [`transitions/developed.md`](transitions/developed.md) | The `developed` transition's prompt, put in front of the test result and the driver's report for the navigator |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, its `test-fast` command, its two agents, the two transitions, and the prompt `/sample_06` — each part with a comment saying what it is for |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the navigator speaks in your
  default group, and step 3 refuses a group another agent already has.
- **`python3` and `make`** on this machine. The tests use Python's own `unittest`;
  nothing to install.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its
own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/06-transitions ~/halyard-samples/cart-transitions
```

```bash
cd ~/halyard-samples/cart-transitions && git init && git add . && git commit -m "Start from the sample"
```

**2. Make a second group, for the driver.** Create a Telegram group, add your bot to
it, and send `/status` there. Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 06 ~/halyard-samples/cart-transitions
```

It asks which group each agent speaks in. For `cart-nav`, the navigator, Enter keeps
your default group (`TELEGRAM_CHAT_ID`); for `cart-drv`, the driver, paste the id
from step 2. Then it adds the project, its two transitions and the prompt
`/sample_06`. Your file is copied first, to `halyard.yaml.<date-time>.bak`, and only
added to, as in sample 01. What it adds comes from
[`halyard.yaml.example`](halyard.yaml.example), where each part has a comment saying
what it is for: open it to see the whole setup before you add it, or to take its
`projects:` and `prompts:` blocks by hand.

**4. Gate it, check it, and restart Halyard** — from the Halyard checkout:

```bash
uv run halyard wire
```

```bash
uv run halyard doctor
```

With no project named, `wire` wires every project in your `halyard.yaml`: this one,
and any of yours, which it leaves as they are once wired. Then restart Halyard, so it
reads the new project: `make restart` if it runs as a service, or stop
`uv run halyard` and start it again.

**5. Open the two agents,** both Claude Code, both in
`~/halyard-samples/cart-transitions`: one session named `cart-transitions-nav`, the
other `cart-transitions-drv`. `uv run halyard sessions` lists the names Halyard can
see, if you would rather put your sessions' own names in the YAML. Each can run on a
model of its own — see [Two agents, two models](#two-agents-two-models).

**6. From your phone.** `/transition` is on the bot's full menu; with
`TELEGRAM_MENU: simple`, type it.

- In the navigator's group, `/prompts`, then the `/sample_06` button — the navigator
  reads `TASK.md` and writes a plan for the driver. It changes nothing.
- In the same group, `/transition`, then **develop** — the plan goes to the driver,
  with `transitions/develop.md` in front. The driver's cards, and its report, arrive
  in the driver's group.
- Once the driver has answered, in its group: `/transition`, then **developed** —
  Halyard runs the tests, then the report and what they printed go to the
  navigator together. The navigator answers in its own group.
- If the navigator says something is missing: in its group, `/transition`, then
  **develop** — its answer goes back to the driver, and round it goes.
- Once the navigator says the task is done, `/commit` in the driver's group — a
  message for the change, on a card, before anything is committed. As in sample 01,
  there is no remote to push to.

## What you will see

- `Hand the last reply here on how?`, with a button for each transition: **develop**
  and **developed**.
- `🤝 develop → cart-drv (driver)…` in the navigator's group.
- `🤝 developed → cart-nav (navigator), running test-fast first…` in the driver's
  group, then `test-fast: passed` — or `failed`. Failing tests go to the navigator
  with the report: it has to see them.
- The navigator's answer in its own group, written from the report, the test result
  and the change.

## Two agents, two models

Two agents are two sessions, so each can run on a model of its own, and the tokens go
where they pay. The navigator takes few turns — read the task, plan, weigh the report
— where thinking hard is worth it. The driver takes many — edits, test runs, another
round — and a lighter model makes those go further. For example:

| Agent | Model | Effort |
|---|---|---|
| `cart-nav`, the navigator | Opus | `xhigh` |
| `cart-drv`, the driver | Sonnet | `max` |

Choose them where you always do: in each session, as you open it in step 5. Halyard
needs nothing for that — a message from your phone resumes the session on the model it
already has.

And when you want to change one without going to the desk, do it from the phone, in
that agent's own group — on the short menu, `/agent` has both as buttons:

- the navigator's group: `/model opus`, then `/effort xhigh`;
- the driver's group: `/model sonnet`, then `/effort max`.

Each answers `Turns started from here will use …`, and holds for that agent's turns
started from the phone until Halyard restarts. `/model default` gives the choice back
to the session, and `/status` shows both: the model in the session, and the one from
here.

## Then

- **Stop the tests, and the transition stops.** A real suite can take minutes. To see
  it here, make one test wait a minute — `import time` and `time.sleep(60)` in it, at
  the desk — and press **developed** again. While `test-fast` runs, `/stop` in the
  driver's group: `⏹ Stopped. transition developed ended, with everything it
  started.`, then `developed stopped before reaching cart-nav (navigator)`. Tests that
  never finished are no result, so nothing is sent. Take the wait out again.
- **Two drivers.** With two agents in the `driver` role, **develop** asks which one,
  and the one the role last worked with is first, in blue.
- `/command`, then **test-fast** — the same tests, on their own, as in sample 05.
  With sample 05's `runs:` and `HALYARD_ALLOW_RISK_AT_OR_BELOW: low`, the driver's own
  `make test-fast` needs no card either.

## Clean up

```bash
uv run halyard unwire cart-transitions
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_06` under `prompts:`, out of your
`halyard.yaml`. A wired project needs Halyard running: with it down, the agent's
commands in that project are denied.
