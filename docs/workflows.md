# Workflows

A handoff hands one reply on and stops there: somebody reads what came back and presses
the next one. A **workflow** presses it. It is a project's handoffs taken in the order
the project wrote them down, each step going to one seat, and each reply's own last line
saying whether the work goes on, comes back a step, or waits for a person.

Everything here sits under the project in `halyard.yaml`, beside the `handoffs:` its
steps name — see [Checks and handoffs](handoffs.md).

```yaml
projects:
  alpha-engine:
    handoffs:
      to_nav: {to: navigator}
      review:
        prompt: NOTES/handoffs/review.md
        followup_prompt: NOTES/handoffs/review-followup.md
        to: reviewer
      reviewed: {to: navigator}
      driver_discover: {prompt: NOTES/handoffs/forward.md, to: driver}
      discover_completed: {prompt: NOTES/handoffs/discovery.md, to: navigator}
      driver_develop: {prompt: NOTES/handoffs/forward.md, to: driver}
      close: {prompt: NOTES/handoffs/close.md, to: navigator}
    workflows:
      steps:
        to_nav:       {seat: nav}
        review:       {seat: xreview, rounds: 2}
        reviewed:     {seat: nav, decided_by: review, rounds: 2}
        discover:     {handoff: driver_discover, seat: xdrv, rounds: 2}
        discover_glm: {handoff: driver_discover, seat: zdrv, rounds: 2}
        discovered:   {handoff: discover_completed, seat: nav, rounds: 2}
        develop:      {handoff: driver_develop, seat: xdrv}
        develop_glm:  {handoff: driver_develop, seat: zdrv}
        close:        {seat: nav}
      level3:    [to_nav, review, reviewed, discover, discovered, develop, close]
      level2glm: [to_nav, discover_glm, discovered, develop_glm, close]
```

## Steps are named once

Every workflow uses the same `steps:`. A step is a handoff going to a seat: `handoff:` is
the step's own name when it says none, and `seat:` can be left out when the handoff's
`to:` already names one seat. One handoff going to two different drivers is two steps —
`discover` and `discover_glm` above — which is where the two are told apart. A workflow
is then a list of step names, in order; `steps` and `decisions` are the two names under
`workflows:` that are not workflows.

A flow naming a step, a step naming a handoff, or a step naming a seat that the project
does not define is refused when the file is read, rather than part-way through a flow.

## Starting one

`/workflow` offers each workflow as a button, then each step of the one pressed: the
work is often past the first step already. The step pressed goes first, carrying the
chat's last reply, the way `/handoff` does. Typed, `/workflow level3 review` starts at
`review` straight away, and anything after the step's name goes in as a note.

## The last line decides

When the seat a step went to replies, Halyard reads the last line of that reply:

- `forward` takes the next step, carrying the reply to it;
- `back` sends the work to the step before — the one that produced what was just judged
  — as that step's next round;
- `wait` stops the run until somebody sends it on;
- anything else carries on to the next step, and the chat says there was no decision
  line.

The label before the word is the project's own: `DECISION: forward`, `RESULT: forward`
and a line that is only `forward` read the same, and so does one a model put in bold. The
same word in the middle of a paragraph decides nothing.

Those three words are the defaults. A project whose prompts ask for other words names
them, and any it leaves out keep their own:

```yaml
    workflows:
      decisions: {forward: go, wait: hold}
```

A project's prompts only have to ask for the line: every step is told where each word
goes.

## A step can act on the decision before it

A reviewer's back is for the navigator to act on. The navigator holds the context, so
the review goes to it whichever way the reviewer decided, and the navigator's reply then
goes where the review said — back to the reviewer, or on to the driver. That is
`decided_by:` on the step after the review: `reviewed` above. The names are the
project's; `decided_by:` names whichever step comes before.

The navigator does not have to write the decision again, and can overrule it by writing
one. A reviewer's wait stops the run before the navigator, as any wait does; sent on
from there, the navigator decides for itself.

## What each step is told

A handoff pressed by hand carries nothing of a workflow. A step's envelope carries the
workflow's own lines as well — where it is, and where each word on its last line would
take the work, with the seat and the round it would be:

```
- Workflow: level3 · step 5 of 7 · discovered
- Decide on your last line: forward (→ develop, xdrv) · back (→ discover, xdrv, round 2 of 2) · wait (→ the operator)
```

The review is told where its decision goes, and the navigator what was decided:

```
- Decide on your last line: forward or back (→ reviewed, nav, who acts on it) · wait (→ the operator)
```

```
- Workflow: level3 · step 3 of 7 · reviewed
- Already decided by review (xreview): back (→ review, xreview, round 2 of 2)
- To overrule it, decide on your last line: forward (→ discover, xdrv) or wait (→ the operator)
```

A step the work came back to also says who sent it, as `Sent back by: nav (navigator) at
18:21`. Coming back is that step's next round, so its handoff's `followup_prompt:` is
what goes in front of it.

## Rounds stop a loop

A step goes once unless its `rounds:` says more. Going back and forth takes two steps
round again, so both say it: `discovered` sending the work back runs `discover` a second
time, and then `discovered` a second time, which is why both have `rounds: 2` above.
`develop` and `close` say nothing, so a `back` at `close` stops and asks.

A step counts every time its handoff has gone for the work item the branch names —
pressed by hand as well, since the seat read it either way — and the envelope's
`Round: n/…` counts against the same number. A step that would go past it does not go:
the run stops, and the chat offers **▶️ Send it anyway** and **⏹ Stop the workflow**.

Two steps naming one handoff share its count, and so does every time it is pressed by
hand. That is why `reviewed` above has a handoff of its own rather than `to_nav`, which
gets pressed for all sorts of things in the life of one piece of work.

## Stopping, and asking where it is

Every step's line in the chat carries **⏹ Stop the workflow**. `/workflow` with a run
going says where it is — `level3 4/7 · discover — waiting for xdrv` — and offers the same
buttons. A run stops, and says why, when a step would go past its rounds, when a seat
decides to wait, and when a step's seat could be more than one seat.

A run is kept per project beside Halyard's database, so a restart picks it up where it
was, and there is one run per piece of work: a project has one working tree, and its
branch says which work that is. A message that reached nobody leaves the run waiting
under the chat's own warning; `/workflow` shows it, and **⏹ Stop the workflow** clears it.
