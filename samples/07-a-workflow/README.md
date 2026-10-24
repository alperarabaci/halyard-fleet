# 07 — A workflow

Sample 06's two transitions, and a third that closes the work, taken in order by
Halyard: a workflow. In sample 06 you pressed each hand-over. Here you press once, to
start, and each agent's last line — `forward`, `back`, `done` — decides where the work
goes next. You are needed for the cards, and when the run stops to ask.

| At the desk | From your phone |
|---|---|
| Hand the navigator's plan to the driver | `/workflow`, then **deliver**, then **develop** |
| Notice the driver has finished, run the tests, take its report to the navigator | Nothing: the driver's last line, `forward`, does it |
| Take what the navigator found back to the driver, and round again | Nothing: `back` does it, as many times as the step's `rounds:` allow |
| Run the tests and an inspection, and take it all to the navigator to close | Nothing: `close` does it, and the navigator's `done` ends the run |

## What is here

| File | What it is |
|---|---|
| [`cart.py`](cart.py) | The app: a shopping cart's total, in cents, with a discount and shipping |
| [`test_cart.py`](test_cart.py) | Its tests. The coupon tests fail until the task is done |
| [`Makefile`](Makefile) | `make test-fast`: the tests |
| [`TASK.md`](TASK.md) | The task: two coupon codes, and any other refused |
| [`PROMPT.md`](PROMPT.md) | What the navigator is asked: a plan for the driver, changing nothing |
| [`transitions/develop.md`](transitions/develop.md) | The `develop` step's prompt, for the driver: make the change, report, decide |
| [`transitions/developed.md`](transitions/developed.md) | The `developed` step's prompt, for the navigator: weigh the report and the tests, decide `forward` or `back` |
| [`transitions/close.md`](transitions/close.md) | The `close` step's prompt, for the navigator: weigh the report, the tests and the inspection, decide `done` or `back` |
| [`inspections/claims.md`](inspections/claims.md) | The `claims` inspection `close` runs: which of the report's claims the change backs |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, its command, inspection, three transitions, the workflow `deliver`, its two agents, and the prompt `/sample_07` — each part with a comment saying what it is for |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 3 asks it something.
- **Earlier samples cleaned up,** if you ran them — the navigator speaks in your
  default group, and step 3 refuses a group another agent already has.
- **`python3` and `make`** on this machine. The tests use Python's own `unittest`;
  nothing to install.
- **The CLI the inspection runs on:** `claude` unless your `HALYARD_INSPECTION_MODEL`
  is a `gpt-…` model, `codex` if it is. Each inspection is a model turn of its own —
  [sample 04](../04-inspections/README.md).

## Run it

**1. Copy the sample out of the Halyard checkout, make it a repository of its own,
and start the task on a branch named for it.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/07-a-workflow ~/halyard-samples/cart-workflow
```

```bash
cd ~/halyard-samples/cart-workflow && git init -b main && git add . && git commit -m "Start from the sample" && git switch -c 12-coupons
```

`12-coupons` is task 12, so the run is known as `cart-workflow#12` — in its report at
the end, and to `halyard runs`. One run per piece of work: the branch says which work
that is.

**2. Make a second group, for the driver.** Create a Telegram group, add your bot to
it, and send `/status` there. Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 07 ~/halyard-samples/cart-workflow
```

It asks which group each agent speaks in. For `flow-nav`, the navigator, Enter keeps
your default group (`TELEGRAM_CHAT_ID`); for `flow-drv`, the driver, paste the id
from step 2. Then it adds the project, with its inspection, transitions and workflow,
and the prompt `/sample_07`. Your file is copied first, to
`halyard.yaml.<date-time>.bak`, and only added to, as in sample 01. What it adds comes
from [`halyard.yaml.example`](halyard.yaml.example), where each part has a comment
saying what it is for: open it to see the whole setup before you add it, or to take
its `projects:` and `prompts:` blocks by hand.

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
`~/halyard-samples/cart-workflow`: one session named `cart-workflow-nav`, the other
`cart-workflow-drv` — each on the model you want for it, as in
[sample 06](../06-transitions/README.md#two-agents-two-models).
`uv run halyard sessions` lists the names Halyard can see, if you would rather put
your sessions' own names in the YAML.

**6. From your phone.** `/workflow` is on the bot's full menu; with
`TELEGRAM_MENU: simple`, type it.

- In the navigator's group, `/prompts`, then the `/sample_07` button — the navigator
  reads `TASK.md` and writes a plan for the driver. It changes nothing.
- In the same group, `/workflow`, then **deliver**, then **develop** — the run starts
  there, carrying the plan to the driver. `/workflow deliver develop` does it in one
  message. From here the run goes by itself:
  - the driver makes the change, reports, and ends with `forward`;
  - `developed` runs the tests and takes the report and their result to the
    navigator, who ends with `forward` — or `back`, and the driver goes again;
  - `close` runs the tests and the `claims` inspection, and the navigator ends with
    `done` — or `back`, to the driver.
- Meanwhile, the driver's cards come to its group: allow them. `/workflow` in either
  group says where the run is.
- Once the run is done, `/commit` in the driver's group — a message for the change,
  on a card, before anything is committed. As in sample 01, there is no remote to push
  to.

## What you will see

- Every step's line in the navigator's group, where the run started, each with
  **⏹ Stop the workflow** under it: `deliver 1/3 · 🤝 develop → flow-drv (driver)…`
- `deliver 2/3 · 🤝 developed → flow-nav (navigator), running test-fast first…`, then
  `test-fast: passed`.
- `deliver 3/3 · 🤝 close → flow-nav (navigator), running test-fast then inspecting
  claims first…`, then `test-fast: passed · claims: answered` — and an
  **INSPECTION** card in the navigator's group for any read of the inspection's that
  the gate does not pass on its own.
- In each agent's session, a first line that says a workflow sent it, and the run's
  own name: `To flow-drv (driver), from Halyard workflow deliver (run 3f2a9c1e),
  step 1 of 3: develop — transition: develop.` Under it, where each word on its last
  line would take the work.
- At the end, in the navigator's group: `📋 deliver is done — cart-workflow#12 · run
  3f2a9c1e`, when it started and finished, and the steps with their rounds —
  `develop x2 · developed x2 · close`, for a run that went back once.

## Then

- **When the run stops to ask.** A reply with no decision, a `wait`, a `forward` at
  `close` where only `done` ends the run, or a `back` past a step's `rounds:` stops
  the run. It says why, with a card that keeps the work in it: what the agents have
  said since, where their words would have taken the work, **🧭 Pick a step**,
  **⏹ Stop the workflow** — and, after a `forward` at the last step, **⏭ On to the
  end**. `/workflow` shows the card again whenever you ask.
- **A wait goes on with the agent that asked.** A driver that ends with `wait` and a
  question gets its answer in its own group; its first reply after that which ends in
  `forward` carries the run on.
- **Every run, step by step** — from the Halyard checkout:

  ```bash
  uv run halyard runs 12
  ```

  When each step reached its agent, which round it was, and what the answer decided.
  `uv run halyard runs` lists the latest runs, a line each.
- `/transition` still offers each transition by hand, as in sample 06, outside the
  run.

## Clean up

```bash
uv run halyard unwire cart-workflow
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_07` under `prompts:`, out of your
`halyard.yaml`. A wired project needs Halyard running: with it down, the agent's
commands in that project are denied.
