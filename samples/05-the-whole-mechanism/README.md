# 05 — The whole mechanism

The things you would walk back to the desk for — run the tests, start the next task on
a branch of its own — as a press or two on your phone. And an agent that runs the
tests itself, without asking you each time.

| At the desk | From your phone |
|---|---|
| `make test-fast`, in the project | `/command`, then **test-fast** |
| `git switch main`, then `git switch -c 12-discount` | `/command`, then **next-task**, then send `12-discount` |
| Allowing the agent's `make test-fast` each time it asks | Nothing: `runs:` trusts it, and its edits still come as cards |

Each command is a name under the project's `commands:`, and a button on your phone.
Halyard runs the one you press on your machine, in the project, and answers in the
group — no agent, no card. A list, like `next-task`, runs several in one press. Later
samples put the same commands inside a transition or a workflow step, so ending a
piece of work — the tests, the inspections, the hand-off — is a press too.

## What is here

| File | What it is |
|---|---|
| [`cart.py`](cart.py) | The app: a shopping cart's total, in cents |
| [`test_cart.py`](test_cart.py) | Its tests. The discount test fails until the task is done |
| [`Makefile`](Makefile) | `make test-fast`: the tests, as the agent runs them. `/command` runs the same tests straight, so that their verdict is the last line |
| [`TASK.md`](TASK.md) | The task: a discount on the total |
| [`PROMPT.md`](PROMPT.md) | The task as the agent is sent it: make the change, then run `make test-fast` |
| [`.gitignore`](.gitignore) | Keeps Python's `__pycache__/` out of your commits |
| [`halyard.yaml.example`](halyard.yaml.example) | What this sample adds to your `halyard.yaml`: the project, its `commands:` and `runs:`, its agent, and the prompt `/sample_05` — each part with a comment saying what it is for |

## Before you start

- **Everything [sample 01](../01-one-agent/README.md#before-you-start) needs,** with
  Halyard running: step 2 asks it something.
- **Earlier samples cleaned up,** if you ran them — the agent speaks in your default
  group, and step 2 refuses a group another agent already has.
- **`python3` and `make`** on this machine. The tests use Python's own `unittest`;
  nothing to install.
- **`HALYARD_ALLOW_RISK_AT_OR_BELOW: low`** in your settings, which `runs:` needs —
  step 3. It is for the whole machine: plain reads inside every wired project
  (`cat`, `grep`, `git log`…) stop asking too.

## Run it

**1. Copy the sample out of the Halyard checkout, and make it a repository of its
own, on `main`.**

```bash
mkdir -p ~/halyard-samples && cp -R samples/05-the-whole-mechanism ~/halyard-samples/shopping-cart
```

```bash
cd ~/halyard-samples/shopping-cart && git init -b main && git add . && git commit -m "Start from the sample"
```

`-b main`, so the branch `next-task` goes back to is there whatever your git calls
its first branch.

**2. Add it to your `halyard.yaml`** — from the Halyard checkout:

```bash
uv run halyard samples add 05 ~/halyard-samples/shopping-cart
```

It asks which group the agent speaks in — Enter keeps your default group
(`TELEGRAM_CHAT_ID`) — and adds the project with its `commands:` and `runs:`, and the
prompt `/sample_05`. Your file is copied first, to `halyard.yaml.<date-time>.bak`, and
only added to, as in sample 01. What it adds comes from
[`halyard.yaml.example`](halyard.yaml.example), where each part has a comment saying
what it is for: open it to see the whole setup before you add it, or to take its
`projects:` and `prompts:` blocks by hand.

**3. Switch on what `runs:` needs, if it is not on yet** — `samples add` never changes
settings. Under `settings:` in your `halyard.yaml`:

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
and any of yours, which it leaves as they are once wired. `doctor` says
`shopping-cart runs without asking: make test-fast` — or, if step 3 is still to do,
that the project trusts commands but the setting is not on. Then restart Halyard, so
it reads both: `make restart` if it runs as a service, or stop `uv run halyard` and
start it again.

**5. Open the agent in `~/halyard-samples/shopping-cart`,** and give the session the
name in `session:`: `shopping-cart`. Leave it in its usual permission mode, not auto
mode — see [Then](#then). `uv run halyard sessions` lists the names Halyard can see,
if you would rather put your session's own name in the YAML.

**6. From your phone,** in your default group. `/command` is on the bot's full menu;
with `TELEGRAM_MENU: simple`, type it.

- `/command`, then **next-task**, and send `12-discount` when it asks — back to
  `main`, and a new branch for the task. `/command next-task 12-discount` does it in
  one message.
- `/command`, then **test-fast** — the tests run on your machine, and fail: `total()`
  takes no discount yet.
- `/prompts`, then the `/sample_05` button — the agent changes `cart.py` and runs
  `make test-fast`. Its edits come to your phone as cards; its test run does not.
- `/command`, then **test-only**, and send `discount` — only the discount test, and
  it passes now.
- `/commit` — Halyard writes a message for the change and shows it on a card before
  anything is committed, on `12-discount`. As in sample 01, there is no remote to push
  to.

## What you will see

- `▶️ Running python3 -m unittest -q…`, then `✅ test-fast finished in 0s` or
  `🚫 test-fast failed after 0s`, with the end of what it printed.
- `✍️ next-task takes task — send it as your next message here.`, then `next-task`
  running `to-main` and `new-branch`, one after the other. Had `to-main` failed,
  `new-branch` would not have run, and the list says so.
- A card for each edit the agent makes, and none for its `make test-fast`. Run any
  other way — `python3 -m unittest -v`, say — it is a card.
- The agent's answer in the group: what `make test-fast` printed.

## Then

- **Trust the agent's other test runs.** `runs:` takes the command as written; to
  trust `python3 -m unittest` with whatever it adds, from the Halyard checkout:

  ```bash
  uv run halyard rules add shopping-cart 'python3 -m unittest *'
  ```

  ```bash
  uv run halyard rules list shopping-cart
  ```

  It applies from the next command, with no restart, and is kept in Halyard's
  database rather than your `halyard.yaml`; `list` says which entry is where. Quote
  the entry: its `*` is for Halyard, not your shell.
- **Auto mode.** A chat that approves for itself — Claude Code's auto mode, Codex's
  "approve for me" — decides its own calls, so Halyard leaves them to it before
  `runs:` is read: no card for anything, and the audit log says `call.left_to_agent`.
  `HALYARD_OVERRIDE_AGENT_APPROVALS: true` in your settings puts every call back on
  Halyard's gate; `uv run halyard doctor` says which way it is.
- `/stop` while a command runs — ends it. A long test run started from the phone can
  be stopped from it too.

## With a GitLab project

Optional, for a copy whose remote is a GitLab project: Halyard reads and writes task
labels on GitLab, and on no other tracker today. Everything above works without it.

- Push the copy's `main` to a GitLab project — `git push -u origin main`, so
  `git pull` knows where to pull from — and open an issue for the task, say #12.
  `gitlab.com` is recognised from the remote; on a GitLab of your own, add
  `forge: gitlab` to the project.
- Under `settings:`, `HALYARD_FORGE_TOKEN`: a token that may label issues. GitLab has
  no scope narrower than `api` for that; a project access token keeps it to one
  project.
- Create the labels `area:total` and `area:discount` in the GitLab project.

Then the project's `commands:` can pull before branching, and take a value from the
task's labels — replace them with these, and add `labels:`:

```yaml
    commands:
      test-fast: python3 -m unittest -q
      test-only: python3 -m unittest -q -k {input.word}
      test-area: python3 -m unittest -q -k {label_groups.area}
      to-main: git switch main
      pull: git pull --ff-only
      new-branch: git switch -c {input.task}
      next-task: [to-main, pull, new-branch]
    labels:
      groups:
        area: [area:total, area:discount]
```

Restart Halyard, and from your phone:

- `/command`, then **next-task**, and send `12-discount` — `main`, brought up to
  date, and a branch named for issue #12: `12-` is how Halyard knows which task the
  branch is for.
- `/command`, then **test-area** — the issue has no `area` label yet, so a card asks
  for one. Press `area:discount`: it goes on the issue, and the tests with "discount"
  in their name run. Next time the issue says which, and nothing is asked.
- `/label` — the issue, its labels, and a button for each it could have.
  `/label area:total` puts that one on outright.

## Clean up

```bash
uv run halyard unwire shopping-cart
```

By name, unlike `wire`: with none, `unwire` takes the gate off every project in the
file, yours too. If you trusted a command with `rules add`, take it back:

```bash
uv run halyard rules remove shopping-cart 'python3 -m unittest *'
```

Then take the project, and `/sample_05` under `prompts:`, out of your `halyard.yaml` —
and `HALYARD_ALLOW_RISK_AT_OR_BELOW`, if you added it only for this sample: it applies
to every wired project. A wired project needs Halyard running: with it down, the
agent's commands in that project are denied.
