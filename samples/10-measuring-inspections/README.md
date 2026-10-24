# 10 — Measuring inspections

Every inspection is a model turn, and in a workflow it runs at every close. A few
weeks in, the questions are practical ones: which inspections find something worth
their tokens, whether a cheaper model would find the same, whether last week's change
to an inspection's prompt made it better or only longer.

Halyard can keep every inspection run — what the model was given, word for word, what
it said, the model and effort, the revision of the inspection's file, how long it
took, and the tokens it used — and read them back. This sample makes a few runs, then
measures them.

| By hand | With Halyard |
|---|---|
| Paste the same report, with the same instructions, into another model, and keep the answers side by side | `halyard inspect repeat <id> --model …`, then `halyard inspect compare <id>` |
| Add up what the inspections cost from each runtime's own accounting | `halyard usage` |
| Remember which version of an inspection's prompt gave which answers | Every kept run carries its file's revision |

## What is here

| File | What it is |
|---|---|
| [`cart.py`](cart.py) | The app: a shopping cart's total, in cents, with a discount and shipping |
| [`test_cart.py`](test_cart.py) | Its tests. The bulk discount tests fail until the task is done — and leave two gaps, on purpose: see [What a good answer finds](#what-a-good-answer-finds) |
| [`Makefile`](Makefile) | `make test-fast`: the tests |
| [`TASK.md`](TASK.md) | The task: a bulk discount, rounded down |
| [`PROMPT.md`](PROMPT.md) | The task as the agent is sent it |
| [`inspections/test-reliability.md`](inspections/test-reliability.md) | The inspection to measure, on Opus: whether the tests would catch the task done wrong |
| [`inspections/over-engineering.md`](inspections/over-engineering.md) | A second, on Sonnet: whether the change is more than the task needs |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: `HALYARD_KEEP_INSPECTIONS`, the project, its two inspections, its agent, and the prompt `/sample_10` — each part with a comment saying what it is for |

## What is kept, and where

In Halyard's database — `halyard.db` in the Halyard checkout, unless
`HALYARD_DB_PATH` says otherwise — two tables:

- **`inspection_runs`**, a row per run, while `HALYARD_KEEP_INSPECTIONS` is on: the
  inspection and its file, `file_version` — the commit that last changed that file,
  or `+ local edits` — the model, effort and runtime, the transition or workflow step
  that ran it, the input word for word, the answer, `took` in seconds, and where the
  files stood. A repeat is a row too, marked `experimental`, with `repeat_of`
  pointing at the run it repeats, so it never mixes with the runs the work made.
- **`turn_usage`**, a row per turn Halyard started itself — inspections, commit
  messages, compaction records — with its tokens, whatever the setting. A run's `id`
  is the `session_id` its tokens are under.

This is the one place Halyard keeps text: the input holds the reply that was
inspected. That is why it is off until you turn it on.

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the agent speaks in your default
  group, and step 2 refuses a group another agent already has.
- **`python3` and `make`** on this machine. The tests use Python's own `unittest`;
  nothing to install.

## Run it

**1. Copy the sample out of the Halyard checkout, make it a repository of its own,
and start the task on a branch named for it.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/10-measuring-inspections ~/halyard-samples/cart-inspections
```

```bash
cd ~/halyard-samples/cart-inspections && git init -b main && git add . && git commit -m "Start from the sample" && git switch -c 14-bulk-discount
```

`14-bulk-discount` is task 14, so the runs are kept as `cart-inspections#14`.

**2. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 10 ~/halyard-samples/cart-inspections
```

It asks which group the agent speaks in — Enter keeps your default group
(`TELEGRAM_CHAT_ID`) — and adds the project with its two inspections, and the prompt
`/sample_10`. Your file is copied first, to `halyard.yaml.<date-time>.bak`, and only
added to, as in sample 01. What it adds comes from
[`halyard.yaml.example`](halyard.yaml.example), where each part has a comment saying
what it is for: open it to see the whole setup before you add it, or to take its
`projects:` and `prompts:` blocks by hand.

**3. Turn on keeping the runs** — `samples add` never changes settings. Under
`settings:` in your `halyard.yaml`:

```yaml
  HALYARD_KEEP_INSPECTIONS: true
```

**4. Gate it, check it, and restart Halyard** — from the Halyard checkout:

```bash
uv run halyard wire
```

```bash
uv run halyard doctor
```

With no project named, `wire` wires every project in your `halyard.yaml`: this one,
and any of yours, which it leaves as they are once wired. Then restart Halyard, so it
reads the project and the setting: `make restart` if it runs as a service, or stop
`uv run halyard` and start it again.

**5. Open the agent in `~/halyard-samples/cart-inspections`,** and give the session
the name in `session:`: `cart-inspections`.

**6. From your phone,** in your default group. `/inspect` is on the bot's full menu;
with `TELEGRAM_MENU: simple`, type it.

- `/prompts`, then the `/sample_10` button — the agent makes the change and reports.
  Its edits and its `make test-fast` come to your phone as cards.
- `/inspect`, then **test-reliability** — the inspection reads the report and the
  tests, and its answer comes back in the group.
- `/inspect`, then **over-engineering** — the same, for the change itself.

Each run is kept. Press them again later, after another change, and those are kept
too.

## Measure them

From the Halyard checkout, at the desk.

**1. What ran** — a line per run: its id, when, the work, the inspection, the
model and effort, how long it took, and how it ended.

```bash
uv run halyard inspect recent
```

**2. Read the answers,** against [what a good answer finds](#what-a-good-answer-finds).

**3. The same input on a cheaper model** — word for word what the first model was
given, twice, to see whether it says the same both times. An id can be cut to any
start no other id has. Halyard must be running: a read of the repeat's that the gate
does not pass on its own is a card on your phone, as for any inspection.

```bash
uv run halyard inspect repeat <id> --model sonnet --effort medium --times 2
```

Nothing is posted and nothing is labelled: the repeats are kept as experiments, beside
the run they repeat, and `recent` marks it `repeated 2x`.

**4. Side by side:**

```bash
uv run halyard inspect compare <id>
```

The original and its repeats, a row each — model, effort, runtime, how long it took,
the outcome, the tokens read and written, the list price, and whether it saw the same
files. The input, the table and every answer are written out as files too, under
`projects/cart-inspections/inspections/<id>/` beside the database, for reading one
against another.

**5. What they cost** — tokens per model and purpose, for the turns Halyard started
itself today: `inspect test-reliability`, its repeats as `inspect test-reliability ·
repeat`, and `inspect over-engineering`.

```bash
uv run halyard usage 1
```

The tokens are the measure. The dollar figure is the runtime's own, at list price: on
a subscription it is what the same turns would cost on the API, not a bill.

**6. A change to the prompt.** A repeat gives the input word for word, and the
inspection's instructions are part of it — so a new prompt is a new run, not a
repeat. Edit [`inspections/test-reliability.md`](inspections/test-reliability.md) in
your copy — ask it, say, to name for each assertion a wrong answer it would still
accept — commit it, and press `/inspect`, then **test-reliability**, again. The new
run carries the file's new revision, and one query lines the revisions up:

```bash
sqlite3 halyard.db "SELECT r.inspection, r.file_version, r.model, COUNT(*), ROUND(AVG(r.took)), SUM(u.output_tokens) FROM inspection_runs r LEFT JOIN turn_usage u ON u.session_id = r.id WHERE r.experimental = 0 GROUP BY r.inspection, r.file_version, r.model;"
```

A revision a row: how many runs, how long they took on average, and how many tokens
they wrote.

## What a good answer finds

The bulk discount tests leave two gaps, on purpose, so the answers have something to
be judged by:

- **`test_a_mix_of_lines` is weak.** It asserts only that the total is above 0: a
  `bulk_discount` that takes 5% off the whole cart, rather than off each line of ten
  or more, passes it.
- **The rounding is not tested.** `TASK.md` asks for whole cents rounded down, and no
  test has an amount that needs rounding: one that rounds up passes every test.

A `test-reliability` answer that names both has earned its tokens. One that calls the
tests reliable has not, whatever it cost — and that is the comparison `compare` and
`usage` let you make, model by model and revision by revision.

## Around the work

Not about measuring, but the same kind of thing: what you would otherwise go to the
desk for.

- **`/doctor` from your phone** — the same check as `uv run halyard doctor`, run by
  Halyard and posted to the group.
- **`/open`** — on a Mac, asks macOS to open the Claude or Codex app, a button each,
  with no card: opening an application changes nothing that has to be undone. It
  opens the app, not a particular session.
- **One zip of everything Halyard keeps** — from the Halyard checkout:

  ```bash
  uv run halyard export
  ```

  `halyard.yaml`, the database — runs, usage and the kept inspections among it — the
  audit log, your prompts, the logs, and the files each project's configuration
  points at, in `export/halyard-state-<date-time>.zip`. It holds your bot token, so
  it is readable by you only. Putting it back is unzipping it into a checkout, with
  Halyard stopped.
- **More runs to measure.** [Sample 08](../08-three-agents/README.md) run with
  `HALYARD_KEEP_INSPECTIONS: true` keeps four inspections at every review and close,
  each run with the workflow step, phase and round it came from.
- **Another family of model.** `--runtime codex --model <a gpt model>` gives the same
  input to GPT, on Codex; `codex debug models --bundled` lists what your CLI can run.

## Clean up

```bash
uv run halyard unwire cart-inspections
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_10` under `prompts:`, out of your
`halyard.yaml` — and `HALYARD_KEEP_INSPECTIONS`, if you turned it on only for this
sample: the runs already kept stay in the database. A wired project needs Halyard
running: with it down, the agent's commands in that project are denied.
