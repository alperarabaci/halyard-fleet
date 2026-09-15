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
    checks:                           # /checks offers these, one button each
      proof: NOTES/checks/proof.md
      destructive: NOTES/checks/destructive.md
    label_groups:                     # the task's label from each group goes on the envelope
      level: [level::1, level::2, level::3]
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
        commands: [test-fast]
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
