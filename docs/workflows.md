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
        verified:     {handoff: to_nav, seat: nav}
        close:        {seat: nav}
      level3:    [to_nav, review, reviewed, [discover, discovered, develop, verified], close]
      level2glm: [to_nav, discover_glm, discovered, develop_glm, close]
```

## Steps are named once

Every workflow uses the same `steps:`. A step is a handoff going to a seat: `handoff:` is
the step's own name when it says none, and `seat:` can be left out when the handoff's
`to:` already names one seat. One handoff going to two different drivers is two steps —
`discover` and `discover_glm` above — which is where the two are told apart. A workflow
is then a list of step names, in order; `steps`, `decisions` and `phases` are the names
under `workflows:` that are not workflows.

A flow naming a step, a step naming a handoff, or a step naming a seat that the project
does not define is refused when the file is read, rather than part-way through a flow.

## Starting one

`/workflow` offers each workflow as a button, then each step of the one pressed: the
work is often past the first step already. The step pressed goes first, carrying the
chat's last reply, the way `/handoff` does. Typed, `/workflow level3 review` starts at
`review` straight away, and anything after the step's name goes in as a note.

A step of the phases can start in a phase other than the first — work that went
through its first part by hand joins the run where it is: `/workflow level3 verified
phase 2`.

## The last line decides

When the seat a step went to replies, Halyard reads the last line of that reply:

- `forward` takes the next step, carrying the reply to it;
- `back` sends the work to the step before — the one that produced what was just judged
  — as that step's next round;
- `wait` stops the run until somebody sends it on;
- `next` starts another phase, and only means something at the end of one — see
  [Phases](#phases-the-steps-that-go-round-going-forward);
- anything else carries on to the next step, and the chat says there was no decision
  line — except at the end of a phase, where it stops.

The label before the word is the project's own: `DECISION: forward`, `RESULT: forward`
and a line that is only `forward` read the same, and so does one a model put in bold. The
same word in the middle of a paragraph decides nothing.

Those four words are the defaults. A project whose prompts ask for other words names
them, and any it leaves out keep their own:

```yaml
    workflows:
      decisions: {forward: go, wait: hold, next: sonraki}
```

A project's prompts only have to ask for the line: every step is told where each word
goes.

## Phases: the steps that go round going forward

A plan in two parts is discovered, developed and verified twice before the work closes.
No number of rounds says that: a round is the same step again because it was not right,
and this is the next part of the plan going through the same steps. So one list inside a
workflow is its **phases** — `[discover, discovered, develop, verified]` in `level3`
above. The steps before it go once, the steps after it go once, and the steps in it go
once per phase.

The last step of the phases decides between two ways on:

- `next` starts the next phase from its first step, carrying the reply to it;
- `next develop` starts it at a later step instead, for a part that has nothing to
  discover — the name has to be one of the phases' steps, and the chat marks the run
  leaving its usual path: `↪️ level3 phase 2 starts at develop, skipping discover,
  discovered — nav named where to start.`;
- `forward` leaves the phases for whatever comes after them — `close` above.

Only there. A `next` at any other step stops the run rather than guess which phase the
seat meant, and so does a phase that ends with **no decision at all**: carrying on would
leave the phases with nobody having said the work was done. The stop offers the next
phase and the way out of them as buttons, so the work stays in the run whichever it is.

A `wait` at the end of a phase stops with the same two buttons. That is the operator's
moment between parts — accepting on screen, publishing, bringing the stack up with what
was just made — and what follows it is the next part or the end of them, not whichever
step happens to come next in the list.

`phases:` under `workflows:` says how many phases go before the run stops and asks —
three unless a project writes another number. Past it, the next phase is the
operator's to send, the way a round past `rounds:` is.

Inside the phases a step's rounds are counted per phase: the second phase's `discover`
starts from its first round, whatever the first phase's took. `back` from the step a
phase started at goes to the end of the phase before, which is where what it was handed
came from.

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
take the work, with the seat, the round and the phase it would be:

```
- Workflow: level3 · step 5 of 8 · discovered
- Phase: 1
- Decide on your last line: forward (→ develop, xdrv) · back (→ discover, xdrv, round 2 of 2) · wait (→ the operator)
```

The last step of the phases is told what `next` does as well:

```
- This step ends phase 1: next (→ discover, xdrv, phase 2) starts the next one, and next <step> starts it at another of discover, discovered, develop, verified; forward leaves the phases. wait, or a reply with no decision, stops for the operator, who starts the next phase or leaves them.
```

The review is told where its decision goes, and the navigator what was decided:

```
- Decide on your last line: forward or back (→ reviewed, nav, who acts on it) · wait (→ the operator)
```

```
- Workflow: level3 · step 3 of 8 · reviewed
- Already decided by review (xreview): back (→ review, xreview, round 2 of 2)
- To overrule it, decide on your last line: forward (→ discover, xdrv) or wait (→ the operator)
```

A step the work came back to also says who sent it, as `Sent back by: nav (navigator) at
18:21`. Coming back is that step's next round, so its handoff's `followup_prompt:` is
what goes in front of it, with the seat's own answer to the round before.

## Rounds stop a loop

A step goes once unless its `rounds:` says more. Going back and forth takes two steps
round again, so both say it: `discovered` sending the work back runs `discover` a second
time, and then `discovered` a second time, which is why both have `rounds: 2` above.
`develop` and `close` say nothing, so a `back` at `close` stops and asks.

**Rounds are the run's own.** Each step counts the times it reached its seat in this
run — per phase, inside the phases — and the envelope's `Round: n/…` counts against the
same number. A handoff pressed by hand is not part of a run and counts nothing: it sends
its own prompt every time, and says no round. Two steps naming one handoff do not share
a count either.

A step that would go past its rounds does not go: the run stops, and the chat offers to
send it anyway.

## Stopping, steering, and asking where it is

Every step's line in the chat carries **⏹ Stop the workflow**. `/workflow` with a run
going says where it is — `level3 5/8 · develop · phase 2 — waiting for xdrv` — and
offers the same button.

A run stops, and says why, when a step would go past its rounds, when the phases would
go past theirs, when a phase ends with no decision, when a seat decides to wait, and when
a step's seat could be more than one seat. The card it stops with keeps the work in the
run:

- **▶️ Send it anyway** — or **↻ Phase 2 at discover** — sends what is ready;
- **⏭ On to close** leaves the phases instead, where a phase just ended or waited;
- **🧭 Pick a step** offers the flow's steps, and the one pressed goes next, with the
  run's rounds and phase kept — a loop it stopped in is not reset by a tap. Typing
  `/workflow level3 develop` does the same for a stopped run;
- **⏹ Stop the workflow** clears it. Starting it again after that starts afresh.

A run is kept per project beside Halyard's database, so a restart picks it up where it
was, and there is one run per piece of work: a project has one working tree, and its
branch says which work that is. A message that reached nobody leaves the run waiting
under the chat's own warning, and counts no round; `/workflow` shows it, and **⏹ Stop
the workflow** clears it.
