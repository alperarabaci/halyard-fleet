# 09 — Fewer cards

On the first day, a card for each command is the point. A week in, most of the
cards are the same few: the tests the agent runs, the notes it writes. You approve
them every time, so the press decides nothing — and the one card that matters
arrives among them.

This sample makes those cards, then lets Halyard propose what to trust, from the
cards you actually answered. You choose what stops coming; everything else keeps
asking.

| By hand | With Halyard |
|---|---|
| Go back through the cards and work out which commands you approve every time | `halyard upkeep runs-advice fewer-cards`: the cards, grouped by command, with a proposal for what to trust and how many cards it would have spared |
| Write each one into `runs:`, spelled so the gate matches it | `halyard rules add …`, the line printed under the proposal. It applies from the next command |
| Approve every note the agent writes | `writes:` lets writes under `NOTES/` through; edits to the code still ask |

## What is here

| File | What it is |
|---|---|
| [`cart.py`](cart.py) | The app: a shopping cart's total, in cents |
| [`test_cart.py`](test_cart.py) | Its tests. They pass; the agent adds one for each task, test first |
| [`Makefile`](Makefile) | `make test-fast`: the tests |
| [`TASK.md`](TASK.md) | Two small tasks — a coupon, then shipping — one per round |
| [`PROMPT.md`](PROMPT.md) | What the agent is sent: the next task, test first, then a note in `NOTES/` |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the setting, `writes:`, the project with nothing trusted yet, its agent, and the prompt `/sample_09` — each part with a comment saying what it is for |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the agent speaks in your default
  group, and step 2 refuses a group another agent already has.
- **`python3` and `make`** on this machine. The tests use Python's own `unittest`;
  nothing to install.
- **`HALYARD_ALLOW_RISK_AT_OR_BELOW: low`** in your settings — step 3. A trusted
  command needs it. It is for the whole machine: plain reads inside every wired
  project (`cat`, `grep`, `git log`…) stop asking too.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its
own.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/09-fewer-cards ~/halyard-samples/fewer-cards
```

```bash
cd ~/halyard-samples/fewer-cards && git init -b main && git add . && git commit -m "Start from the sample"
```

**2. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 09 ~/halyard-samples/fewer-cards
```

It asks which group the agent speaks in — Enter keeps your default group
(`TELEGRAM_CHAT_ID`) — and adds the project and the prompt `/sample_09`. Your file
is copied first, to `halyard.yaml.<date-time>.bak`, and only added to, as in
sample 01. What it adds comes from [`halyard.yaml.example`](halyard.yaml.example),
where each part has a comment saying what it is for. It does not add `writes:` —
that comes after the first round.

**3. Switch on what a trusted command needs, if it is not on yet** — `samples add`
never changes settings. Under `settings:` in your `halyard.yaml`:

```yaml
  HALYARD_ALLOW_RISK_AT_OR_BELOW: low
```

**4. Gate it, check it, and restart Halyard** — from the Halyard checkout:

```bash
uv run halyard wire
```

```bash
uv run halyard doctor
```

With no project named, `wire` wires every project in your `halyard.yaml`: this one,
and any of yours, which it leaves as they are once wired. Then restart Halyard:
`make restart` if it runs as a service, or stop `uv run halyard` and start it again.

**5. Open the agent in `~/halyard-samples/fewer-cards`,** and give the session the
name in `session:`: `fewer-cards`. Leave it in its usual permission mode, not auto
mode: a chat that approves for itself sends Halyard no cards to count.

**6. The first round, from your phone,** in your default group: `/prompts`, then the
`/sample_09` button. The agent adds the coupon, test first, and each edit, test run
and note is a card — allow each one:

- each edit — `test_cart.py`, `cart.py`, the tick in `TASK.md`;
- each test run — `python3 -m unittest -q test_cart.…`, and `make test-fast`;
- the note, `NOTES/coupon.md`.

Its reads — `cat`, `ls`, `git diff` — are not cards: the setting lets them through.

## Choose what to trust

From the Halyard checkout, at the desk.

**1. What came as cards** — the evidence, with no model asked:

```bash
uv run halyard upkeep runs-advice fewer-cards --evidence
```

Halyard reads its own log of the last 14 days, and judges each card again by
today's rules. Among what it prints:

```
Shell commands that came as cards: 4 — allowed 4, denied 0, timed out 0, unanswered 0, other 0.
…
Other cards, not shell commands and not part of this: 4.
…
Still cards today, by family — 2 of 2 families shown:
- python3: 3 cards (allowed 3, denied 0, timed out 0, unanswered 0; approximate 0)
    e.g. 'python3 -m unittest -q test_cart.CouponTest.test_save10_takes_10_percent_off'
- make test-fast: 1 cards (allowed 1, denied 0, timed out 0, unanswered 0; approximate 0)
```

Your counts will differ: they are the cards your agent's round made. The "other
cards" are the edits and the note — file writes, which this job leaves to you and
`writes:`.

**2. What to trust** — the same evidence, read by a model that proposes entries:

```bash
uv run halyard upkeep runs-advice fewer-cards
```

The model gets the evidence and nothing else: a Claude Code turn with no tools, so it
cannot open your files or your `halyard.yaml`. It proposes; Halyard checks each
proposal the way it checks an entry, counts from its log what it would have spared,
and prints the line that adds it. Something like:

```
  python3 -m unittest *
    would have spared 3 of the cards
    why: the project's own test runner; its arguments are test names and options
    halyard rules add fewer-cards 'python3 -m unittest *'

  make test-fast
    would have spared 1 of the cards
    why: the project's whole test suite, the target named exactly
    halyard rules add fewer-cards 'make test-fast'
```

The proposals and the reasons are the model's; the counts and the lines are
Halyard's. A proposal that could run anything — `python3 *` — comes back as `not an
entry`, with no line to add it. Each run is kept: `uv run halyard upkeep recent`
lists them, and `uv run halyard upkeep show <id>` prints one again.

**3. Add the ones you agree with.** Nothing is added for you:

```bash
uv run halyard rules add fewer-cards 'python3 -m unittest *'
```

```bash
uv run halyard rules add fewer-cards 'make test-fast'
```

Each applies from the agent's next command, with no restart. Quote the entry: its `*`
is for Halyard, not your shell. A last `*` takes options and paths inside the
project, so `python3 -m unittest -q test_cart.ShippingTest` goes through, and
`python3 -c '…'` is still a card.

**4. Let the notes through.** At the top of your `halyard.yaml` — beside `settings:`
and `projects:`, not under the project:

```yaml
writes:
  - NOTES/**
```

Then restart Halyard: `writes:` is read when it starts.

## The second round

`/sample_09` again, from your phone. The agent does the next task — shipping — the
same way, and the cards that come are the edits: `test_cart.py`, `cart.py`, the tick
in `TASK.md`. Its test runs and `NOTES/shipping.md` go through with none. Each is
written to Halyard's audit log with what let it through — the entry, or the pattern.

## What you trusted

- **A test runner runs the tests the agent wrote.** Trusting `python3 -m unittest *`
  lets the agent run its own test code without asking — what it buys is no card for
  a run you approved every time anyway. Its edits are still yours to see.
- **`NOTES/**` is matched inside every wired project,** and only there: a path that
  climbs out with `..` or through a link is a card however it is spelled. `.git/` and
  the folders the runtimes read their hooks from always ask, whatever the pattern.
- **Approving often is a reason to look, not a reason to trust.** The model is asked
  for the narrowest entry the evidence shows, and Halyard says how many of the cards a
  proposal would spare were denied, timed out or never answered. Which to add is
  yours.

## Then

- **Why each went through** — the last grants in this project, from the Halyard
  checkout:

  ```bash
  sqlite3 halyard.db "SELECT recorded_at, action, COALESCE(json_extract(detail, '$.command'), json_extract(detail, '$.path')), COALESCE(json_extract(detail, '$.why'), json_extract(detail, '$.pattern')) FROM audit_log WHERE project = 'fewer-cards' AND action IN ('risk.preauthorized', 'write.preauthorized') ORDER BY sequence DESC LIMIT 10;"
  ```

  A row a call: the command or the file, and the entry, read or pattern that let it
  through.
- **Another model for the advice.** It runs on `HALYARD_INSPECTION_MODEL` when that
  model runs on Claude Code, and on Sonnet otherwise. An `upkeep:` block in
  `halyard.yaml` names another, and how far back to read — see
  [Upkeep](../../docs/upkeep.md).
- **The same trust on another machine.** `uv run halyard rules export trusted.txt`,
  then `uv run halyard rules import trusted.txt` there.

## Clean up

```bash
uv run halyard unwire fewer-cards
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Take back what you trusted:

```bash
uv run halyard rules remove fewer-cards 'python3 -m unittest *'
```

```bash
uv run halyard rules remove fewer-cards 'make test-fast'
```

Then take the project, and `/sample_09` under `prompts:`, out of your `halyard.yaml` —
and `writes:` and `HALYARD_ALLOW_RISK_AT_OR_BELOW`, if you added them only for this
sample: each applies to every wired project. A wired project needs Halyard running:
with it down, the agent's commands in that project are denied.
