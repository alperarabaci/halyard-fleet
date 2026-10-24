# Configuring a project

What goes through without a card, and what else a project sets for itself in
`halyard.yaml`. [Setup](setup.md) has the file's shape; inspections and transitions
have [a page of their own](transitions.md), and workflows [another](workflows.md).

## What goes through without a card

Three grants, each off until you turn it on, each written to the audit log with what
allowed it.

**The commands a project trusts.** With `HALYARD_ALLOW_RISK_AT_OR_BELOW: low`, a shell
command the gate understands whole inside the project goes through: plain reads —
`cat`, `grep`, `git log` — and the commands the project names under `runs:`, or adds
with `halyard rules add`, written as the agent types them:

```yaml
projects:
  alpha-engine:
    runs:
      - make test-fast
      - uv run pytest *               # a last `*`: options and paths inside the project
```

An entry that could run anything — `bash *`, `python3 -c *`, `make *` — is refused. Not
sure what to trust? `halyard upkeep runs-advice <project>` reads the cards the project
still asks, has a model propose entries, and shows how many cards each would have
spared, counted from the log, with the `halyard rules add` line beside it. You add the
ones you agree with — [sample 09](../samples/09-fewer-cards/), and
[Upkeep](upkeep.md).

**Files the agent writes.** A turn you start from the phone runs headless, where Claude
Code cannot open its own permission dialog — so an unlisted `Write` used to be denied
outright, with nothing to answer and no sign of why. Now it asks. Name the places that
should not have to ask, at the top of `halyard.yaml`:

```yaml
writes:
  - NOTES/**
```

Patterns are matched *inside the project the write belongs to* — a path that climbs out
with `..` or through a symlink is refused however it is spelled. The gate's own files
always ask, whatever the pattern: `.git/` and the directories the runtimes read their
hooks from, so even `**` cannot let an agent switch the gate off. opencode's edits are
matched the same way, and a change to several files at once goes through only when
every one of them may.

**MCP calls and web fetches** are gated the same way, and for the same reason: at the
desk they are a popup, and from a phone they were denied with nothing to answer. An MCP
server is mostly read-only queries that one turn calls dozens of times, so name those
rather than approving each:

```yaml
tools:
  - mcp__*__list_*
  - mcp__*__get_*
```

One pattern covers the same tool on a local server and a production one. `Bash` and the
file tools cannot be granted here — the first is what the gate is for, and the second is
granted by destination under `writes:`.

## Commands, labels and commits

Each is one line under the project in `halyard.yaml`:

```yaml
projects:
  alpha-engine:
    path: ~/code/alpha-engine
    commands:                         # what /command offers, by name
      test-all: make test-all
      test-fast: make test-fast
      bootstrap: make bootstrap-up
    validate: test-fast               # one of commands:, run first by /review_and_commit
    labels:                           # everything about task labels
      offer: [andon, rework]          # narrows /label; empty means all of them
      exclude: [wontfix]              # never offered by /label, as patterns
    warn_if: [task-id-missing]        # the default; [] turns the warnings off
    confirmation:                     # the extra round, when a guard cannot catch it
      inquiry: NOTES/CONFIRMATION_INQUIRY.md
      review: NOTES/CONFIRMATION_REVIEW.md
```

`/command` runs in the background and reports the tail when it finishes, one at a time
per project. Exit 0 is passed and any other code failed, unless the command lists it
under `not_run:` — a transition stops at a command that did not run, rather than hand on
a result that is not there. In a transition, a command's last line is the one shown and
the only one its inspections read, so a command that ends on its own verdict reads
better than one that ends on `make: *** … Error 1`. `/label` reads the task number off
the branch and asks its issue tracker — `HALYARD_FORGE_TOKEN` is the only thing it
needs, and only a host that cannot name itself needs `forge:` as well.

`/commit` takes the whole working tree, has a message written for it in this
repository's own style, and shows what changed rather than only which files — and
stops there, which is what most changes want. `/review_and_commit` is the same command
with everything the project asked for: the command `validate:` names runs, the warnings
apply, and the round is offered. A failing one means no card at all.

**`confirmation:` buys a round that a guard cannot.** A test proves what it tests and a
file of invariants proves nothing at all — an agent's attention is finite, and past a
point more written rules are noise competing with the work. So the commit card offers a
third answer beside committing and not committing: send the round. `inquiry` is put in
front of the model while it writes the message and asks it one more thing — is *this*
change worth another look? — and any flag it raises is lifted out of the message and
shown above the card. `review` is the round itself, and goes to the project's navigator
when the button is pressed; nothing is committed, and committing afterwards is a fresh
`/commit` that reads the branch again. Both files belong to the project, because what
is worth asking again is something a team learns about its own failures.

## When a session is compacted

When a session is compacted, half its context goes and what it loses is the
expensive half: which numbers were measured rather than claimed, what you already
rejected and why, the corrections it made to its own earlier mistakes. What comes back
is a confident agent working from a slightly older version of events.

Two files per agent close that, and neither needs you to be watching:

```yaml
agents:
  nav:
    runtime: claude-code
    before_compaction: compaction_hook/precompact.md
    after_compaction: compaction_hook/postcompact.md
```

`before_compaction` is instructions for a record Halyard writes *about* the session as
it is compacted — a separate one-shot turn reads the transcript, so the live session is
never resumed or forked. `after_compaction` is what the session is handed once the
summary is done. Both are optional and per agent: a navigator holding a plan needs them,
a driver running one command does not.

Measured before it was built: a hook cannot steer the summary itself — output from
`PreCompact` is refused by the runtime as an injection attempt — and `SessionStart`
after a compaction is the one channel that reaches the model. The whole path is best
effort, so a record that is late or a file that is missing costs orientation, never the
session.

## The models Halyard's own turns run on

They are settings. The compaction record is
written on `HALYARD_DEFAULT_MODEL` at `HALYARD_DEFAULT_EFFORT` — sonnet at the runtime's
own effort unless set — and `HALYARD_COMPACTION_MODEL` and `HALYARD_COMPACTION_EFFORT`
override it. Commit wording has its own pair, `HALYARD_COMMIT_MODEL` and
`HALYARD_COMMIT_EFFORT`; each unset field inherits its `HALYARD_DEFAULT_*`
counterpart. A small model can write commit messages while inspections and compaction
keep their choices:

```yaml
settings:
  HALYARD_COMMIT_MODEL: gpt-6-luna
  HALYARD_COMMIT_EFFORT: low
```

A `gpt-…` model runs on Codex. These settings are read when the service starts, and
`halyard doctor` checks the chosen models and efforts against the installed runtime's
catalog. Usage is recorded under `commit message`, `compaction record`, or the
inspection's name, so `halyard usage` can compare the measured tokens after a change.

---

[← Back to the README](../README.md)
