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

`low` is the only value the setting takes. Anything else — `medium` would let commits,
moves and container starts through — is ignored, and the gate stays as strict as with
nothing set. The service still starts, and says so: in its log, in the navigator's chat
and in `halyard doctor`. Refusing the value used to stop the service from starting,
which denied every command on every wired project and told nobody on the phone.

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
    commit_options:                   # what /commit can check first, a button each
      fast: {commands: [test-fast]}
      wide: {commands: [test-all], inspect: [destructive]}   # from inspections:
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
better than one that ends on `make: *** … Error 1`.

**`/label` reads the task number off the branch** — `320-rag-v4-pdf-report` as GitLab
names a branch made from an issue, `42-label-tasks-on-github` as GitHub does — and asks
the issue tracker the repository's remote points at. `gitlab.com` and `github.com` name
themselves; a GitLab or a GitHub Enterprise of your own needs `forge: gitlab` or
`forge: github` on the project. Each forge has a token of its own under `settings:`,
used for that forge alone, so one machine can label a GitHub project and a GitLab one:

```yaml
settings:
  HALYARD_GITHUB_TOKEN: "github_pat_…"    # github.com and GitHub Enterprise
  HALYARD_GITLAB_TOKEN: "glpat-…"         # gitlab.com and a GitLab of your own
```

- **GitHub:** a fine-grained personal access token limited to the repository, with the
  repository permission **Issues: read and write**. That is what GitHub's documentation
  implies reading an issue, listing and creating labels, and labelling an issue need;
  it was not measured. GitHub has no scoped labels, so a colon is part of a name, and a
  label the repository lacks — one `labels: who_worked` writes, say — is created first.
- **GitLab:** `api` scope, since GitLab has none narrower for writing a label onto an
  issue; a project access token keeps it to one project.

`HALYARD_FORGE_TOKEN`, the name GitLab's token had while it was the only forge, still
works: it is read as GitLab's when `HALYARD_GITLAB_TOKEN` is unset, and never as
GitHub's. `halyard doctor` says to rename it. For a project that asks for labels —
`labels: who_worked` or `labels: groups` — it also names the token its forge needs
when that is not set; for any other project, `/label` says which token is missing
when somebody uses it.

`/commit` takes the whole working tree, has a message written for it in this
repository's own style, and shows what changed rather than only which files. A project
without `commit_options:` gets that card at once, which is what most changes want. A
project with them is asked first: the plain **Commit**, which checks nothing, or one of
its options. `/commit wide` names the option outright.

**A commit option checks the change before there is a card.** Its `commands:` run first,
in order, as a transition's do: one project command at a time, `/stop` ends one, and a
task label is filled in or asked for. One that fails or does not run means no card, and
the end of what it printed. Then its `inspect:` inspections run side by side over the
change. What they are given to read is what Halyard can say of it — `git diff --stat
HEAD` and the untracked files `git add -A` would take, not the diff, which can be huge;
they run `git diff` themselves — with the envelope a transition's inspections have. Each
answer is posted above the card and refuses nothing, and the card says how each came
out. That card carries the project's warnings and offers its round.

**An inspection that judges a report does not belong in a commit option.** One that
checks what an agent claimed — that its tests passed, that the proof is there — has no
report to read at commit time, only the change. Name the inspections that read the code.

**What is committed is what was checked.** A commit takes the tree as it is at the tap,
so an option holds it to the files it checked, by the tree id `git write-tree` gives
them. Files that change while the option runs mean no card; files that change after it
mean the tap commits nothing and says so. A command that rewrites files — a formatter —
changes them too: that run gets no card, and the next, with nothing left to rewrite,
does. The plain Commit checks nothing and is held to nothing.

**Documentation alone runs nothing.** When every changed file is prose — `*.md`,
`docs/`, `NOTES/` and the like — an option runs neither its commands nor its
inspections, and the card says so. The warnings still apply, and the files are held as
above, so code that arrives afterwards is not committed unchecked.

`validate: test-fast`, the one check a project had before options, is read as
`commit_options: review: commands: [test-fast]`, and `halyard doctor` says so.
`/review_and_commit` is answered when typed and is off the menu: it runs a project's one
option, offers its options without the plain Commit when there are several, and in a
project without any gives the warnings and the round alone.

**`confirmation:` buys a round that a guard cannot.** A test proves what it tests and a
file of invariants proves nothing at all — an agent's attention is finite, and past a
point more written rules are noise competing with the work. So the card a commit option
makes offers a third answer beside committing and not committing: send the round. `inquiry` is put in
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
