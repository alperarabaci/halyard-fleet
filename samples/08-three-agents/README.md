# 08 — Three agents

Everything the earlier samples showed, working together on a project that has
something to protect. Two bounded contexts, a catalog and the cart that buys from it,
and three agents: a navigator and a driver on Claude, and a reviewer on GPT, through
Codex. One `/workflow` takes a task from a plan, through a review and two parts, to
close — and at every hand-over the project's own checks have their say.

## Why it takes three agents and a guard

An agent given a task focuses on it, makes it work, and moves on — and it does work.
What it does not weigh is everything else: the boundary it crossed to get there, the
copy of another context's data it now keeps, the test it bent to fit. Telling it every
rule in every prompt does not hold either: a context window holds only so much, and a
long list of rules is noise that buries the few that matter.

So the rules that matter most are written once, short, and checked where the work
passes:

| What | Watches for | When |
|---|---|---|
| [`AGENTS.md`](AGENTS.md) | Five invariants, and no more | Read by every agent, every turn |
| [`scripts/guard.py`](scripts/guard.py) | One context reaching inside another, or using one it may not — by file and line | Every `test-fast` |
| [`contracts/`](contracts/) | The catalog changing what the cart relies on | Every `test-fast` |
| The `proof` inspection, on Sonnet, and `over-engineering`, on GPT | A premise in the plan the code does not bear out; more than the task needs | The `review` step, on the plan, before any code exists |
| The reviewer, on GPT | A plan that breaks an invariant, weighing what the two inspections found | The `review` step |
| The `test-reliability` inspection, on Opus | Tests that would not catch the task done wrong, or were bent to fit it | The `close` step, on the change |
| The `bounded-context` inspection, on GPT | What no script sees: the cart keeping its own stock, a price worked out in the cart | The `close` step, on the change |

A guard says yes or no about what can be checked mechanically, every time. A model
reads for what cannot. Two kinds of model, Claude and GPT, look at the same work from
two sides — and none of it costs you more than a press on your phone.

| At the desk | From your phone |
|---|---|
| Check the plan's premises and its scope, hand it to a second model for review, and take the answer back to the navigator | `/workflow`, then **deliver**, then **review** |
| Send the revised plan back for a second round | Nothing: the review's `back` does it |
| Hand each part of the plan to the driver, and run the guard, the contract and the tests after each | Nothing: `forward` and `next` do it, and `developed` runs `test-fast` |
| Before closing, run every check again, and have the tests and both contexts looked over | Nothing: `close` does it, and the navigator's `done` ends the run |

## What is here

| File | What it is |
|---|---|
| [`catalog/`](catalog/) | The catalog context: [`__init__.py`](catalog/__init__.py), its public face, and [`products.py`](catalog/products.py), its data |
| [`cart/`](cart/) | The cart context: [`__init__.py`](cart/__init__.py). Prices come from the catalog |
| [`tests/`](tests/) | Each context's own tests: [`test_catalog.py`](tests/test_catalog.py) and [`test_cart.py`](tests/test_cart.py). The stock tests fail until the task is done |
| [`contracts/`](contracts/) | [`test_cart_needs_catalog.py`](contracts/test_cart_needs_catalog.py): what the cart needs from the catalog, written by the cart and run against the real catalog — a consumer-driven contract |
| [`scripts/guard.py`](scripts/guard.py) | The boundary guard |
| [`scripts/validate.sh`](scripts/validate.sh) | Runs the parts of `test-fast` one by one, and answers the way Halyard reads a command |
| [`Makefile`](Makefile) | `make test-fast`: the guard, the contract and the tests, in that order |
| [`AGENTS.md`](AGENTS.md) | The five invariants. Codex reads it as it is; [`CLAUDE.md`](CLAUDE.md) points Claude Code at it |
| [`TASK.md`](TASK.md) | The task: stock, which crosses both contexts |
| [`PROMPT.md`](PROMPT.md) | What the navigator is asked: a plan in two parts, one per context |
| [`transitions/`](transitions/) | Each step's prompt: [`review.md`](transitions/review.md) and [`review-followup.md`](transitions/review-followup.md) for the reviewer; [`reviewed.md`](transitions/reviewed.md), [`developed.md`](transitions/developed.md) and [`close.md`](transitions/close.md) for the navigator; [`develop.md`](transitions/develop.md) for the driver |
| [`inspections/`](inspections/) | Four inspections: [`proof.md`](inspections/proof.md) and [`over-engineering.md`](inspections/over-engineering.md) read the plan at review; [`test-reliability.md`](inspections/test-reliability.md) and [`bounded-context.md`](inspections/bounded-context.md) read the change at close |
| [`compaction/`](compaction/) | The navigator's two: [`before.md`](compaction/before.md), what the record of its session keeps, and [`after.md`](compaction/after.md), what it is told once compacted |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, its command, four inspections, five transitions, the workflow `deliver`, its three agents — the navigator with its compaction files — and the prompt `/sample_08` — each part with a comment saying what it is for |

## What the project gives Halyard

Halyard reads two things a project writes, and the project has to write them on
purpose: what a command answers, and how a reply ends.

**A command answers with its exit code and its last line.** `0` passed; anything else
failed, unless the command lists the code under `not_run:` — then it did not run, and
a transition stops there rather than hand on a result that is not one. The envelope
the inspections read carries one line of a command, its last; the agent receiving it
gets the last 5 lines when it passed, and 25 when it failed. `make` alone gets both
wrong: it exits 2 for any failure, and its last line is its own,
`make: *** … Error 1`. So the project's `test-fast` is
[`scripts/validate.sh`](scripts/validate.sh): each part the Makefile lists after
`test-fast:` in turn, each answering 0, 1 or 2, and one last line with all of them:

```
test-fast: exit 1 — boundaries: every boundary kept (2 contexts, 3 files), exit 0; contract: FAILED (errors=1), exit 1; unit: not run (stopped after contract)
```

One command, every check — the way a Makefile gathers many targets under one name,
so that from the phone it is one press, not one per check.

**A reply ends with its decision.** A workflow reads the last line of each reply and
nothing else. `DECISION: forward` moves the work on, `back` returns it, `wait` stops
for you; `next` starts another phase, and at the last step only `done` ends the run.
The label before the colon is the project's own, and so is the language of the words,
under `workflows: decisions:`. A line after the decision makes it no longer the last,
and the run stops. So every step's prompt here ends the same way: the decision alone
on the last line, nothing after it, the reason one line above a `back` or a `wait`,
and `wait` when unsure.

## The navigator keeps its place

The navigator holds the run together: the plan, which part is done, what the review
left open, what you turned down. When its session is compacted, half its context
goes, and the half that goes is the expensive one — which numbers were measured
rather than claimed, which BLOCKER is still open. What comes back is a confident
navigator working from an older version of events.

So the navigator, and only it, has two files:

- [`compaction/before.md`](compaction/before.md) — what the record Halyard writes
  about the session keeps, most important first: what is waiting, the step and the
  part, what was checked and what only claimed, the review's open findings, your
  decisions, its own corrections. A one-shot turn of its own reads the transcript and
  writes it, on `HALYARD_DEFAULT_MODEL` — Sonnet unless set;
  `HALYARD_COMPACTION_MODEL` chooses another for the record alone.
- [`compaction/after.md`](compaction/after.md) — what the session is told once the
  summary is done, under that record: answer what is waiting before anything else,
  measure nothing again, and the few rules that always hold.

The driver and the reviewer need neither: every step hands them what they need, in
its own message. `halyard wire` puts the hooks this needs in place for Claude Code.
None of it asks you anything, and a record that is late, or a file that is missing,
costs the navigator its bearings, never the session.

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 3 asks it something.
- **Earlier samples cleaned up,** if you ran them — the navigator speaks in your
  default group, and step 3 refuses a group another agent already has.
- **`claude` and `codex`** on this machine: Codex for the reviewer and for the two
  inspections on GPT, Claude Code for the rest. `codex debug models --bundled` lists
  what your CLI can run; if it has no `gpt-6.1-sol`, put one it lists in
  `halyard.yaml` — `uv run halyard doctor` fails a model the CLI cannot run.
- **`python3` and `make`.** The tests use Python's own `unittest`; nothing to install.
- **`HALYARD_ALLOW_RISK_AT_OR_BELOW: low`** in your settings — sample 05. The reviewer
  and the inspection read a great deal, and each plain read inside the project would
  otherwise be a card on your phone.

## Run it

**1. Copy the sample out of the Halyard checkout, make it a repository of its own,
and start the task on a branch named for it.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/08-three-agents ~/halyard-samples/shop
```

```bash
cd ~/halyard-samples/shop && git init -b main && git add . && git commit -m "Start from the sample" && git switch -c 12-stock
```

`12-stock` is task 12, so the run is known as `shop#12`.

**2. Make two more groups,** one for the driver and one for the reviewer. Add your bot
to each, and send `/status` there: Halyard answers with the group's id —
`This chat: -100… — no agent speaks here` — which the next step asks for.

**3. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 08 ~/halyard-samples/shop
```

It asks which group each agent speaks in. For `shop-nav`, the navigator, Enter keeps
your default group (`TELEGRAM_CHAT_ID`); for `shop-drv` and `shop-rev`, paste the ids
from step 2. Then it adds the project, with its command, inspection, transitions and
workflow, and the prompt `/sample_08`. Your file is copied first, to
`halyard.yaml.<date-time>.bak`, and only added to, as in sample 01. What it adds comes
from [`halyard.yaml.example`](halyard.yaml.example), where each part has a comment
saying what it is for: open it to see the whole setup before you add it, or to take
its `projects:` and `prompts:` blocks by hand.

**4. Gate it, check it, and restart Halyard** — from the Halyard checkout:

```bash
uv run halyard wire
```

With no project named, `wire` wires every project in your `halyard.yaml`: this one,
for both runtimes its agents use, and any of yours, which it leaves as they are once
wired. Codex runs a hook only once you have trusted it, and skips one you have not
without a word — so open Codex in the project, and choose **Review hooks** when it
asks at startup, as in [sample 02](../02-two-agents/README.md):

```bash
cd ~/halyard-samples/shop && codex
```

```bash
uv run halyard doctor
```

Then restart Halyard, so it reads the new project: `make restart` if it runs as a
service, or stop `uv run halyard` and start it again.

**5. Open the three agents,** all in `~/halyard-samples/shop`:

- Claude Code, a session named `shop-nav` — on Opus, say;
- Claude Code, a session named `shop-drv` — on Sonnet
  ([two agents, two models](../06-transitions/README.md#two-agents-two-models));
- Codex — the one step 4 opened — with its thread named `shop-rev`.

**6. From your phone.** `/workflow` is on the bot's full menu; with
`TELEGRAM_MENU: simple`, type it.

- In the navigator's group, `/prompts`, then the `/sample_08` button — the navigator
  reads `TASK.md` and `AGENTS.md`, and writes a plan in two parts, catalog then cart.
  It changes nothing.
- In the same group, `/workflow`, then **deliver**, then **review** — the run starts
  there, carrying the plan to the reviewer. From here the run goes by itself:
  - **review** — `proof` and `over-engineering` read the plan first; then the plan
    and what they found go to the reviewer, who reads it against `AGENTS.md` and the
    code, and answers with its findings — each a BLOCKER, a FIX or a NOTE — and
    `forward` or `back`;
  - **reviewed** — the review reaches the navigator whichever way it went. After a
    `back`, the navigator answers each BLOCKER and the plan goes back to the
    reviewer, whose second round has `review-followup.md` in front and its own
    findings from the first. After a `forward`, the navigator writes the first part
    for the driver;
  - **phase 1, the catalog** — `develop` takes the part to the driver; `developed`
    runs `test-fast` and takes the report and its result to the navigator, who
    writes the second part and decides `next`. The cart's own tests still fail
    here: they are the second part's work;
  - **phase 2, the cart** — the same two steps; now `test-fast` passes, and the
    navigator decides `forward`;
  - **close** — `test-fast` again, then the `test-reliability` and
    `bounded-context` inspections; the navigator decides `done`, or `back` to the
    driver.
- Meanwhile, each agent's cards come to its own group: allow them. `/workflow` in any
  of the three groups says where the run is.
- Once the run is done, `/commit` in the driver's group — a message for the change, on
  a card, before anything is committed. As in sample 01, there is no remote to push to.

## What you will see

- Every step's line in the navigator's group, where the run started, each with
  **⏹ Stop the workflow** under it: `deliver 1/5 · 🤝 review → shop-rev (reviewer),
  inspecting proof, over-engineering first…`, then `proof: answered ·
  over-engineering: answered`; inside the phases, `deliver 3/5 · phase 1 · 🤝
  develop → shop-drv (driver)…`.
- The reviewer's findings in its own group, starting `REVIEW · round 1/2`.
- After each `developed` and at `close`, a line such as `test-fast: failed` — then the
  navigator reading why in the last line of `test-fast`, part by part.
- `deliver 5/5 · 🤝 close → shop-nav (navigator), running test-fast then inspecting
  test-reliability, bounded-context first…`, then `test-fast: passed ·
  test-reliability: answered · bounded-context: answered`.
- An **INSPECTION** card for any read of an inspection's that the gate does not pass
  on its own — in the group of the agent the step goes to: the reviewer's at
  `review`, the navigator's at `close`.
- At the end, in the navigator's group: `📋 deliver is done — shop#12 · run …`, when
  it started and finished, and the steps it took — `review · reviewed · phase 1:
  develop, developed · phase 2: develop, developed · close`.

## Then

- **Cross a boundary on purpose,** once the work is committed. In `cart/__init__.py`,
  read a price straight from `catalog.products.PRICES`, then `/command`, then
  **test-fast**: `boundaries: 1 crossing: cart/__init__.py:… reaches into
  catalog.products, which catalog does not offer, exit 1`, and nothing after it runs.
  In a run, `developed` and `close` carry that line to the navigator, who sends the
  work back. `git checkout -- cart` puts the file back.
- **A check that cannot run stops the run.** Move `catalog/__init__.py` aside, and
  the guard cannot look: it exits 2, and so does `test-fast`, which `not_run: [2]`
  reads as "did not run". `/command` shows it as failed, with that last line; in a
  run, `developed` or `close` stops there, and nothing reaches the navigator — checks
  that never ran are no result. Put the file back, and pick the step again from
  `/workflow`.
- **Watch the navigator keep its place.** At the desk, `/compact` in the navigator's
  session while a run is going: Halyard writes the record as the compaction starts,
  and hands it back with `compaction/after.md` once it is done. Ask the navigator
  where the work stands, and it answers from the record.
- **Two rounds, then you.** A review that sends the plan back a second time would go
  round a third — past `rounds: 2` — so the run stops and asks; so does a `back` past
  any step's rounds. Rounds running out is not an approval.
- **Every run, step by step** — from the Halyard checkout:

  ```bash
  uv run halyard runs 12
  ```

  Each step with its round, its agent and what was decided — `back (review)` where the
  navigator's reply went where the review said.

## Clean up

```bash
uv run halyard unwire shop
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. Then take the project, and `/sample_08` under `prompts:`, out of your
`halyard.yaml` — and `HALYARD_ALLOW_RISK_AT_OR_BELOW`, if you added it only for these
samples: it applies to every wired project. A wired project needs Halyard running:
with it down, the agent's commands in that project are denied.
