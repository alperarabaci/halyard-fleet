# Inspections and handoffs

Two things a project can add once the basics work, and nothing else needs
either of them. An **inspection** is the project's own text, run over a seat's
reply as a turn of its own: `/inspect` offers each one, and its answer comes
back with a button per seat to hand it on. A **handoff** carries a reply from
one seat to the next the way the project defines it: `/handoff` puts the
project's prompt in front, runs its commands and then its inspections, and
delivers all of it together.

Inspections were called checks until 2026-09-23. A configuration that still
says `checks:` works as it did, `/checks` still answers, and `halyard doctor`
says what to rename.

Everything here sits under the project in `halyard.yaml`, and every file it
names is the project's own, read relative to its `path:`.

```yaml
projects:
  alpha-engine:
    path: ~/code/alpha-engine
    commands:                         # what /command offers; a handoff names these too
      test-fast: make test-fast
      e2e: make test-e2e SCOPE={label_groups.area}   # takes the task's area label
    inspections:                      # /inspect offers these, one button each
      proof: NOTES/inspections/proof.md
      destructive: NOTES/inspections/destructive.md
    label_groups:                     # the task's label from each group goes on the envelope
      level: [level::1, level::2, level::3]
      area: [area:api, area:web, area:all]
    label_findings:                   # an answer saying one of these labels the task halyard:<inspection>
      - "status: candidate"
    handoffs:                         # /handoff offers these, one button each
      review:                         # a prompt, handed to the reviewer
        prompt: NOTES/handoffs/review.md
        to: reviewer
      discovery:                      # a report, back to the navigator, inspected first
        prompt: NOTES/handoffs/discovery.md
        inspect: [proof, destructive]
        to: navigator
      close:                          # the delivery: the tests run, then the inspections
        prompt: NOTES/handoffs/close.md
        commands: [test-fast, e2e]
        inspect: [proof, destructive]
        to: navigator
```

A handoff carries the chat's last reply unless it says
`include_last_message: false`, and goes to the seat or role in `to:` — every
seat is offered when it names none. An inspection, a command or a seat it names that
the project does not define is refused when the file is read, rather than when
somebody presses the button from a phone.

## An inspection runs inside the project

Each is a one-shot turn in the project's own directory, over its own text and
the reply: it can read files, has no tool that edits one, and is told to run
only the commands its text names. Each of those comes to you as a card headed
**INSPECTION**, naming the inspection and the handoff it runs for, in the chat
the handoff is going to — or, for `/inspect`, the chat that asked. **Deny**
refuses one command and the inspection carries on; **Stop the inspection**
refuses it and ends the inspection, along with anything it started. A command
refused, an inspection stopped, or one that runs out of time leaves the
inspection unmeasured rather than clean.

## An inspection runs on the machine's model, or its own

Every inspection runs on one model, and thinks as hard as one effort says —
set for the machine, in the `settings:` of `halyard.yaml`:

```yaml
settings:
  HALYARD_INSPECTION_MODEL: sonnet    # sonnet unless set
  HALYARD_INSPECTION_EFFORT: max      # low, medium, high, xhigh or max
```

Without an effort, the runtime picks one, and not the same for every model: in
the catalog Claude Code 2.1.280 ships, Sonnet 5 thinks at `high` unless told and
Opus 5.5 at `medium`.

An inspection that needs a stronger model says so where it is described,
written as a mapping instead of a file alone. What it leaves out is the
machine's:

```yaml
    inspections:
      proof: NOTES/inspections/proof.md
      bounded-context:
        file: NOTES/inspections/bounded-context.md
        model: opus                   # at the machine's effort, since it names none
```

It runs on that model wherever it runs — from `/inspect`, a handoff or a
workflow's step. An effort the runtime does not take is left out, so the
inspection still runs, at the model's own; `halyard doctor` names it.

## Every inspection and handoff carries an envelope

What Halyard reads for itself: the machine, the branch, HEAD, the files' own
tree id — what `git write-tree` gives for the working tree, so anybody can
compute it and compare — and, kept as the reply came in, where the files stood
then, so an inspection can tell at once whether it is still looking at the code the
report was about. It is of the files, not the commits: committing them leaves
it as it was, and a clean tree's is `HEAD^{tree}`.

What is changed on top of HEAD is there too, with the untracked files counted
apart — `2 files changed, 27 insertions(+), 1 deletion(-) · 3 untracked files`
— because git's own summary leaves them out, and "nothing" beside three new
files reads as no change at all.

A project can name groups of task labels under `label_groups:`, and the one the
task carries from each group goes on the envelope too — `level: level::3`. It
only reports: a task with none of them, or a tracker that cannot be read, adds
nothing.

## A finding can label the task

A project writes under `label_findings:` what its inspections' answers say when
they found something, in its own words — quoted, because a phrase with a colon
is otherwise a YAML mapping — and whenever an answer says one of them the task
gets `halyard:<inspection>`. From `/inspect` and from a handoff alike, since
both run the same inspection. Halyard only adds: closing a finding, as a false
alarm or as approved, is a label somebody puts on by hand. A project that has
not said what a finding looks like has nothing written.

## Every inspection can be kept

Turned on with one setting, and off until it is:

```yaml
settings:
  HALYARD_KEEP_INSPECTIONS: true
```

Then each run — by `/inspect`, by a handoff, by a workflow's step — is a row in
`inspection_runs`, in Halyard's database beside the tokens the turns Halyard
starts use: the inspection, its file and the revision of it, the model, what the
model was given word for word, what it said, how long it took, whether it
answered, the finding phrase it said if any, and where the files stood — `HEAD`
and the content id from the envelope. A run stopped or left unanswered is kept
too, with why. This is the one place Halyard keeps text: the input holds the
reply that was inspected, and an inspection cannot be compared or run again
without it.

The row's id is the session the turn ran under, which is the key its tokens are
recorded by, and a run a workflow's step made carries that step — its run, name,
phase and round — so both join without guessing:

```sql
SELECT r.inspection, r.model, r.took, u.output_tokens, u.cache_read_tokens
FROM inspection_runs r JOIN turn_usage u ON u.session_id = r.id;

SELECT s.step, s.phase, s.round, r.inspection, r.finding
FROM inspection_runs r JOIN workflow_steps s
  ON s.run_id = r.workflow_run AND s.step = r.step AND s.round = r.round
 AND (s.phase IS r.phase);
```

`experimental` and `repeat_of` are for runs made on purpose to compare models —
see the next section — so they never mix with the ones the work made.

Keeping never holds anything up: the row is written off to one side once the
answer is in, so a database busy with the audit log cannot keep an answer from
reaching anybody, and a row that cannot be written costs the row alone.

## A kept inspection can be run again

To see what another model makes of the same job, a kept run can be given to it
again — word for word what the first model was given — from the command line.
An experiment, apart from the work: nothing is posted, nothing is labelled, and
the new row is marked `experimental`, with `repeat_of` pointing at the run it
repeats.

```sh
uv run halyard inspect recent                          # the last runs, with their ids
uv run halyard inspect repeat 5f0c9a2e --model opus    # the same input, another model
uv run halyard inspect repeat 5f0c9a2e --effort max    # the same model, thinking harder
uv run halyard inspect repeat 5f0c9a2e --times 3       # the same again, three times
uv run halyard inspect compare 5f0c9a2e                # the runs side by side
```

A repeat changes only what it is told to: a model or an effort left out is the
original's, so one thing changes at a time — and `--effort default` hands the
effort back to the runtime. Runs kept before efforts were recorded show
`default`, which is what they ran at.

An id can be cut to any start of it that no other id has. A repeat runs on
Claude Code in the project's directory, with the tools an inspection has, one
run after another: a command its model asks to run comes to Telegram as a card
from its session — whose start it prints — and with Halyard stopped it is
refused. Its tokens are recorded as `inspect <name> · repeat`, apart from the
work's.

The files are where they are when it runs; nothing is checked out. The model
reads the envelope the first run was given, and the row keeps where the files
stand now, so `compare` can say whether a repeat saw the same code: `same` is
`yes` when `HEAD` and the content id both match the original's. `compare` also
writes the input, the table and every answer to files — beside the database,
under `projects/<project>/inspections/<id>/` — for whoever judges the runs.

## A handoff can run the project's commands first

Under `commands:` a handoff names entries of the project's `commands:`, and they
run in that order before its inspections — one at a time, as `/command` runs them, so
a handoff does not go while something else is running in the project. What
each did goes into the envelope the inspections read, as one line with its exit code
and last line, and what it printed goes into the message. A command that fails
is reported and the handoff goes on: whoever receives it has to see that it
failed. One still running after ten minutes is stopped, and its line says so.

The inspections read that one line and nothing more, so a command whose results
an inspection has to compare — several targets, each with a count and an exit code —
says all of them on its last line. The seat receiving the handoff gets the tail
of the output as well: the last five lines when the command passed, the last
twenty-five when it failed.

## A command can take a value from the task's labels

Some commands need to know what the work is about — which part of the codebase
an end-to-end suite covers. The task says so: somebody labels it when the work is
planned, from a group the project lists under `label_groups:`, and a command
names that group in full — `SCOPE={label_groups.area}` above. What goes in the
line is the part of each label after its last colon, every one the task carries
from the group, in the group's order: `area:api` and `area:web` run
`make test-e2e SCOPE=api,web`. The envelope's `Ran e2e: …` line shows the line as
it ran.

A task carrying none of the group's labels is asked for one before anything runs:
a button per label, and the one pressed goes on the task, so the next time — or
the other machine — is not asked again. A branch that names no task has nothing
to put it on, and the label pressed is kept for that work until Halyard restarts.
The same happens wherever the command runs: `/command`, a handoff, a workflow's
step, which waits for the tap.

A command naming a group the project does not define, or one with no labels, is
refused when the file is read; so is `validate:` naming one, since a commit has
nowhere to ask.

## A command can be a list, and take a value you type

What comes after a piece of work is often a few commands in a row — tidy up, then
move to the next task — and one of them needs something only you know:

```yaml
    commands:
      cleanup: make cleanup
      pull-branch: make pull-branch TASK={input.task}
      next-task: [cleanup, pull-branch]
```

A list runs its commands one after another, as `/command` runs one, and the
first that does not pass stops the rest; the chat says which did not run.
`{input.task}` is typed by you: `/command next-task` asks for `task` before
anything runs, and your next message in that chat answers — for a few minutes,
and not once another command has been started there. `/command next-task 369`
gives it outright. What you type goes into the line as one word for the shell,
whatever it holds; whether it is a sensible task number is the command's own
business.

`/command` offers a list beside the commands, and only `/command` runs one. A
handoff names commands themselves, so naming a list there is refused when the
file is read, and so is a handoff or `validate:` naming a command that asks for
a typed value: neither has anybody to ask.

## Rounds are a workflow's

A reply sent back comes round again. When it does inside a
[workflow](workflows.md), that is the step's next round: the envelope says which
— `Round: 2/2` — and a handoff can name a second text for every round after the
first:

```yaml
      review:
        prompt: NOTES/handoffs/review.md
        followup_prompt: NOTES/handoffs/review-followup.md
        to: reviewer
```

From the second round on that one goes in front instead of `prompt:`, and the
seat is handed its own answer to the round before, ahead of the reply — so a
reviewer asked again sees what it found last time rather than being set to find
everything afresh. A handoff without one sends its `prompt:` every round.

A handoff pressed by hand counts no rounds and says none. It is not part of a
run, so there is no loop for a count to stop, and a `1/2` on it would be a
workflow's word where there is no workflow: pressed twice, it sends its own
`prompt:` twice. The rounds a workflow counts are its run's own, per step — see
[Rounds stop a loop](workflows.md#rounds-stop-a-loop).
