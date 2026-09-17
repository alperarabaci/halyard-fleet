# Checks and handoffs

Two things a project can add once the basics work, and nothing else needs
either of them. A **check** is the project's own text, run over a seat's reply
as a turn of its own: `/checks` offers each one, and its answer comes back with
a button per seat to hand it on. A **handoff** carries a reply from one seat to
the next the way the project defines it: `/handoff` puts the project's prompt
in front, runs its commands and then its checks, and delivers all of it
together.

Everything here sits under the project in `halyard.yaml`, and every file it
names is the project's own, read relative to its `path:`.

```yaml
projects:
  alpha-engine:
    path: ~/code/alpha-engine
    commands:                         # what /command offers; a handoff names these too
      test-fast: make test-fast
      e2e: make test-e2e SCOPE={label_groups.area}   # takes the task's area label
    checks:                           # /checks offers these, one button each
      proof: NOTES/checks/proof.md
      destructive: NOTES/checks/destructive.md
    label_groups:                     # the task's label from each group goes on the envelope
      level: [level::1, level::2, level::3]
      area: [area:api, area:web, area:all]
    label_findings:                   # an answer saying one of these labels the task halyard:<check>
      - "status: candidate"
    handoffs:                         # /handoff offers these, one button each
      review:                         # a prompt, handed to the reviewer
        prompt: NOTES/handoffs/review.md
        to: reviewer
      discovery:                      # a report, back to the navigator, checked first
        prompt: NOTES/handoffs/discovery.md
        checks: [proof, destructive]
        to: navigator
      close:                          # the delivery: the tests run, then the checks
        prompt: NOTES/handoffs/close.md
        commands: [test-fast, e2e]
        checks: [proof, destructive]
        to: navigator
```

A handoff carries the chat's last reply unless it says
`include_last_message: false`, and goes to the seat or role in `to:` — every
seat is offered when it names none. A check, a command or a seat it names that
the project does not define is refused when the file is read, rather than when
somebody presses the button from a phone.

## A check runs inside the project

Each is a one-shot turn in the project's own directory, over its own text and
the reply: it can read files, has no tool that edits one, and is told to run
only the commands its text names. Each of those comes to you as a card headed
**CHECKER**, naming the check and the handoff it runs for, in the chat the
handoff is going to — or, for `/checks`, the chat that asked. **Deny** refuses
one command and the check carries on; **Stop the check** refuses it and ends
the check, along with anything it started. A command refused, a check stopped,
or one that runs out of time leaves the check unmeasured rather than clean.

## Every check and handoff carries an envelope

What Halyard reads for itself: the machine, the branch, HEAD, the files' own
tree id — what `git write-tree` gives for the working tree, so anybody can
compute it and compare — and, kept as the reply came in, where the files stood
then, so a check can tell at once whether it is still looking at the code the
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

A project writes under `label_findings:` what its checks' answers say when they
found something, in its own words — quoted, because a phrase with a colon is
otherwise a YAML mapping — and whenever an answer says one of them the task gets
`halyard:<check>`. From `/checks` and from a handoff alike, since both run the
same check. Halyard only adds: closing a finding, as a false alarm or as
approved, is a label somebody puts on by hand. A project that has not said what
a finding looks like has nothing written.

## A handoff can run the project's commands first

Under `commands:` a handoff names entries of the project's `commands:`, and they
run in that order before its checks — one at a time, as `/command` runs them, so
a handoff does not go while something else is running in the project. What
each did goes into the envelope the checks read, as one line with its exit code
and last line, and what it printed goes into the message. A command that fails
is reported and the handoff goes on: whoever receives it has to see that it
failed. One still running after ten minutes is stopped, and its line says so.

The checks read that one line and nothing more, so a command whose results a
check has to compare — several targets, each with a count and an exit code —
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
a button per label, and the one pressed goes on the task, so a second round — or
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

## A handoff counts its rounds

A reply sent back comes round again, and every time a handoff goes for the same
work item is a round. The envelope says which — `Round: 2/2` — and so does the
line in the chat it was sent from. The work item is the one the branch names; a
branch without a number counts under its own name. A prompt edited between
rounds is the same work going round again, so the count carries on.

A handoff can name a second text for every round after the first:

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

Two rounds is what the count expects. A third still goes when somebody presses
for it, as `3/2`: every round is a person pressing a button, and an envelope
saying it is past the usual is worth more than a refusal.

A round counts when the seat's session takes the message. One that reached
nobody is not a round, so pressing again is the same round rather than the
next. The count is kept on the machine, beside Halyard's database; a work item
that moves to another machine starts again there.
