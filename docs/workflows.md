# Workflows

A transition hands one reply on and stops there: somebody reads what came back and presses
the next one. A **workflow** presses it. It is a project's transitions taken in the order
the project wrote them down, each step going to one agent, and each reply's own last line
saying whether the work goes on, comes back a step, or waits for a person.

Everything here sits under the project in `halyard.yaml`, beside the `transitions:` its
steps name — see [Inspections and transitions](transitions.md).

```yaml
projects:
  alpha-engine:
    transitions:
      to_nav: {to: navigator}
      review:
        prompt: NOTES/transitions/review.md
        followup_prompt: NOTES/transitions/review-followup.md
        to: reviewer
      reviewed: {to: navigator}
      driver_discover: {prompt: NOTES/transitions/forward.md, to: driver}
      discover_completed: {prompt: NOTES/transitions/discovery.md, to: navigator}
      driver_develop: {prompt: NOTES/transitions/forward.md, to: driver}
      close: {prompt: NOTES/transitions/close.md, to: navigator}
    workflows:
      steps:
        to_nav:       {agent: nav}
        review:       {agent: xreview, rounds: 2}
        reviewed:     {agent: nav, decided_by: review, rounds: 2}
        discover:     {transition: driver_discover, agent: xdrv, rounds: 2}
        discover_glm: {transition: driver_discover, agent: zdrv, rounds: 2}
        discovered:   {transition: discover_completed, agent: nav, rounds: 2}
        develop:      {transition: driver_develop, agent: xdrv}
        develop_glm:  {transition: driver_develop, agent: zdrv}
        verified:     {transition: to_nav, agent: nav}
        close:        {agent: nav, works_on: develop}
      level3:    [to_nav, review, reviewed, [discover, discovered, develop, verified], close]
      level2glm: [to_nav, discover_glm, discovered, develop_glm, close]
```

## Steps are named once

Every workflow uses the same `steps:`. A step is a transition going to an agent: `transition:` is
the step's own name when it says none, and `agent:` can be left out when the transition's
`to:` already names one agent. One transition going to two different drivers is two steps —
`discover` and `discover_glm` above — which is where the two are told apart. A workflow
is then a list of step names, in order; `steps`, `decisions` and `phases` are the names
under `workflows:` that are not workflows.

A flow naming a step, a step naming a transition, or a step naming an agent that the project
does not define is refused when the file is read, rather than part-way through a flow.

A step whose agent holds another role than the one its transition goes to — the driver's
`driver_discover` given to a navigator — is said when the file is read, in the log and by
`halyard doctor`. It still goes where it says, since it may be meant; usually it is a label
on the wrong line, and the step goes to the wrong agent in every run.

## Starting one

`/workflow` offers each workflow as a button, then each step of the one pressed: the
work is often past the first step already. The step pressed goes first, carrying the
chat's last reply, the way `/transition` does. Typed, `/workflow level3 review` starts at
`review` straight away, and anything after the step's name goes in as a note.

A step of the phases can start in a phase other than the first — work that went
through its first part by hand joins the run where it is: `/workflow level3 verified
phase 2`.

## The last line decides

When the agent a step went to replies, Halyard reads the last line of that reply:

- `forward` takes the next step, carrying the reply to it;
- `back` sends the work to the step before — the one that produced what was just judged
  — as that step's next round, or to the step it works on, for one that says
  `works_on:`;
- `wait` stops the run until the agent that asked to wait decides again, or somebody
  sends it on — see [Stopping](#stopping-steering-and-asking-where-it-is);
- `next` starts another phase, and only means something at the end of one — see
  [Phases](#phases-the-steps-that-go-round-going-forward);
- `done` ends the workflow, and only at its last step — see
  [The end is done](#the-end-is-done);
- a reply with none of them stops the run, the way a wait does: every step is told the
  words it decides in, so a reply without one is something gone wrong — a step sent to
  the wrong agent, a turn that ended on a question — and carrying it on handed the next
  agent a reply never meant for it. A review whose decision the next step acts on is the
  exception: it goes to that step whichever way it went — see
  [A step can act on the decision before it](#a-step-can-act-on-the-decision-before-it).

The label before the word is the project's own: `DECISION: forward`, `RESULT: forward`
and a line that is only `forward` read the same, and so does one a model put in bold. The
same word in the middle of a paragraph decides nothing.

Those five words are the defaults. A project whose prompts ask for other words names
them, and any it leaves out keep their own:

```yaml
    workflows:
      decisions: {forward: go, wait: hold, next: sonraki, done: closed}
```

A project's prompts only have to ask for the line: every step is told where each word
goes.

## The end is done

Leaving the last step says the work is finished. That is a claim, so it has a word of
its own: at the workflow's last step only `done` ends the run.

- A `forward` there, written out of habit, or a reply with no decision, stops the run.
  The card offers **⏭ On to the end**, which ends it.
- The agent's own `done` afterwards ends it too, the way a wait goes on — see
  [Stopping](#stopping-steering-and-asking-where-it-is).
- Anywhere else "Done." is as often the end of a sentence as a decision, so it decides
  nothing there, like any other sentence.

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
- `forward` leaves the phases for whatever comes after them — `close` above. Where
  nothing comes after them, `done` ends the workflow instead.

Only there. A `next` at any other step stops the run rather than guess which phase the
agent meant, and so does a phase that ends with **no decision at all**: carrying on would
leave the phases with nobody having said the work was done. The stop offers the next
phase and the way out of them as buttons, so the work stays in the run whichever it is.

A `wait` at the end of a phase stops with the same two buttons, and the agent's own `next`
or `forward` afterwards does what they do. That is the operator's moment between parts —
accepting on screen, publishing, bringing the stack up with what was just made — and what
follows it is the next part or the end of them, not whichever step happens to come next in
the list.

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

## A step can work on an earlier step's work

`close` comes after `verified`, and both go to the navigator: the navigator reads the
driver's report, decides the work can close, and close goes to it next. Left at
that, what close carries is the navigator's own "this can close" — and that is the
text its inspections read, where pressed by hand from the driver's chat they would
read the driver's report. Its `back` would hand the navigator its own reply too.

`works_on:` names the step whose work this one takes:

```yaml
        close:        {agent: nav, works_on: develop}
```

- It carries that step's agent's last reply — the last thing said in the driver's
  chat, its report — as `/transition` pressed there would.
- Its `back` goes to that step, in the phase the work was made in: what close found
  is the driver's to fix. From there the run comes forward as ever — `develop`, then
  `verified`, then `close` again, on the driver's new report.
- Sent back to itself, it carries what sent it back, as any step does.

The step it names has to come before it in every workflow that takes it, which is
checked when the file is read. A step either acts on the decision before it or works
on an earlier one: `decided_by:` and `works_on:` together are refused, since the
review it is meant to act on would never reach it.

## What each step is told

A transition pressed by hand carries nothing of a workflow. A step says on its first line
that a Halyard workflow sent it — something a project's prompts can point at, to tell an
agent to keep going and to put any question in its reply:

```
To xdrv (driver), from Halyard workflow level3 (run 3f2a9c1e), step 6 of 8: develop — transition: driver_develop.
```

`3f2a9c1e` is the run's own name. Every `Workflow …` line in Halyard's log carries it, and
so does the line that says where a run is, so one run can be found by itself — `grep
3f2a9c1e logs/halyard.log`, `halyard runs 3f2a9c1e` — whatever its messages say the work is.

Its envelope carries the workflow's own lines as well — where it is, and where each word
on its last line would take the work, with the agent, the round and the phase it would be:

```
- Workflow: level3 · step 5 of 8 · discovered
- Phase: 1
- Decide on your last line: forward (→ develop, xdrv) · back (→ discover, xdrv, round 2 of 2) · wait (→ the operator)
```

The last step of the phases is told what `next` does as well:

```
- This step ends phase 1: next (→ discover, xdrv, phase 2) starts the next one, and next <step> starts it at another of discover, discovered, develop, verified; forward leaves the phases. wait, or a reply with no decision, stops for the operator, who starts the next phase or leaves them.
```

The workflow's last step is told `done` in place of `forward`:

```
- Workflow: level3 · step 8 of 8 · close
- Decide on your last line: done (→ the workflow ends) · back (→ develop, xdrv, round 2 of 1, which waits for the operator) · wait (→ the operator)
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
18:21`. Coming back is that step's next round, so its transition's `followup_prompt:` is
what goes in front of it, with the agent's own answer to the round before.

## Rounds stop a loop

A step goes once unless its `rounds:` says more. Going back and forth takes two steps
round again, so both say it: `discovered` sending the work back runs `discover` a second
time, and then `discovered` a second time, which is why both have `rounds: 2` above.
`develop` and `close` say nothing, so a `back` at `close`, which goes to `develop`, stops
and asks.

**Rounds are the run's own.** Each step counts the times it reached its agent in this
run — per phase, inside the phases — and the envelope's `Round: n/…` counts against the
same number. A round is counted as the turn carrying the step starts, so an answer back in
seconds marks its own round and is weighed with it counted. A step that reached nobody, or
whose turn ended without an answer, gives its round back: sending it again is the same
round. A transition pressed by hand is not part of a run and counts nothing: it sends
its own prompt every time, and says no round. Two steps naming one transition do not share
a count either.

A step that would go past its rounds does not go: the run stops, and the chat offers to
send it anyway.

## Stopping, steering, and asking where it is

Every step's line in the chat carries **⏹ Stop the workflow**. `/workflow` with a run
going says where it is — `level3 5/8 · develop · phase 2 — waiting for xdrv` — and
offers the same button.

**A step is waited on once it gets there.** Its commands and inspections run first, and a
message to an agent in the middle of a turn waits behind that turn. Until a turn carrying
the step starts in the agent's session, the run says the step is *on its way to xdrv*, and
what the agent says in the meantime answers something else: it moves nothing.

**A run's card says what its step is doing.** Under the line the step started with, the
card keeps one more, rewritten as the step moves:

```
⏳ close_x — running test-fast · 12 min
⏳ close_x — inspecting · 4 of 6 · 3 min
⏳ close_x — on its way to xnav · under a minute
⏳ close_x — waiting for xnav · 25 min
```

The time is how long the step has been at what the line says, from a clock — so a command
that prints nothing still shows how long it has run — and a command that does print has
its newest line under its time. Its inspections are counted as they answer, since they
run side by side. The time moves once a minute, and once every ten after the first hour.
The line stops when the card closes: the next step's card, or the one the run stops with,
says the rest. A step's command no longer posts a message of its own every 45 seconds;
a transition pressed by hand, which has no run card, still does.

**A step on its way can be called back.** **⏹ Stop the workflow** clears the run, as it
always has. Steering it sends the run to another step instead: **🧭 Pick a step** on the
card `/workflow` shows while a step is on its way, or a typed `/workflow level3 develop`.
Either way the step on its way stops where it is — a command it is running ends as `/stop`
ends one, with everything it started, and its inspections stop with any card they had
open. Its message never goes: every runtime is asked, right before the turn that would
carry it, whether the run still sends it. The chat says the step was stopped before it
reached its agent. Once a step got there it is waited on: Stop still clears the run, and
the agent's answer then moves nothing.

**A run has one card with buttons at a time: its newest.** When the next step goes, the
run stops, or `/workflow` is asked, the card before says *This run has a newer card* and
loses its buttons. They name only the workflow, so left live they would act on whatever
run is going when pressed. A button on a card of a run that is over or was replaced — one
left in the chat when the checkout moved on to another task — is not acted on: its card
closes, saying it is from an earlier run.

A run stops, and says why, when a step would go past its rounds, when the phases would
go past theirs, when a reply decides nothing, when an agent decides to wait, when the last
step ends without `done`, when a step's agent could be more than one agent, when a step
reaches nobody, and when Halyard restarts while a step is on its way. The card
it stops with — and the same card again whenever `/workflow` is asked — keeps the work in
the run:

- **💬 xdrv forward: developed → nav** — what the run's agents have said since it stopped.
  A stopped run is moved by its card, not by a reply, and the work often goes on outside it:
  an agent's later decision is offered from each of that agent's steps, since which one it
  finished is not something the run can know, and the button carries that agent's reply;
- **▶️ forward: developed → nav** and **↩️ back: discover → xdrv** — after a wait or a
  reply that decided nothing, where the agent's own words would have taken the work, the
  step and its agent named. A move a button cannot make as it is — into another phase,
  or past a step's rounds — is left to picking a step;
- **▶️ develop → xdrv** — or **↻ Phase 2 at discover** — sends what is ready, named;
- **⏭ On to close** leaves the phases instead, where a phase just ended or waited, and
  **⏭ On to the end** ends the workflow where its last step ended without `done`;
- **🧭 Pick a step** offers the flow's steps, and the one pressed goes next, with the
  run's rounds and phase kept — a loop it stopped in is not reset by a tap. Typing
  `/workflow level3 develop` does the same for a stopped run;
- **⏹ Stop the workflow** clears it. Starting it again after that starts afresh.

**A wait goes on with the agent that asked for it,** and so do a reply that decided
nothing and a last step that ended without `done`. What the agent stopped for is usually
a word with you, in its own chat — the card says so.

- Its replies until then decide nothing, and another `wait` is the same one.
- The first reply that ends in a decision — `forward`, `back`, `next`, or `done` at the
  last step — is its decision at the step it stopped at. The run goes on from there,
  carrying that reply.
- The card's buttons still move the run without that decision. Once the run has been
  sent on or steered, the agent's later decisions move nothing.
- Read back later, the round keeps what its reply said: `wait`, or the `forward` that
  stopped the last step.

## When a run ends

A run that goes all the way says so in the chat it was started from, in three lines —
when it started and finished, how long that was, and the steps it took with their rounds.
Every card it had says so too — *The run is done*, or *The run was stopped* — and loses
its buttons:

```
📋 level3phased is done — alpha-engine#386
14:02 → 17:40 · 3 h 38 min
to_nav · review x2 · reviewed x2 · phase 1: discover, develop, developed · phase 2: develop, developed · close
```

The same run is kept in Halyard's database, beside the tokens the turns Halyard starts
use: one row per run in `workflow_runs` (`project`, `work`, `workflow`, `started_at`,
`finished_at`, `outcome`, `phases`, `deliveries`) and one per step delivered in
`workflow_steps` (`at`, `step`, `phase`, `round`, `agent`, `decision`, `decided_by`). A run
stopped with **⏹** is kept too, as `stopped`, without the report.

`decision` is what the run did on that step's answer — `forward`, `back`, `wait`, `next` or
`done`, whatever words the project's prompts use for them — and `decided_by` is whose word it was:
the step itself, or the one before it for a step that acts on it (`decided_by:`). Both are
empty for an answer that decided nothing or never came, and for steps kept before
2026-09-25.

`halyard runs` reads them back without SQL: the latest runs a line each, or one piece of
work's runs step by step — named by the work or by the run's own name. A run not finished
yet, waiting on an agent, on its way to one or stopped for you, is read from its project's
file, so it shows before it ends.

```
$ halyard runs #386
alpha-engine#386 · level3 · done
09-25 12:00 → 12:35 · 35 min

  at     step      round  agent  decided
  12:00  review    1      xrev   back
  12:05  reviewed  1      nav    back (review)
  12:20  review    2      xrev   forward
  12:30  reviewed  2      nav    forward (review)
```

The tables join the tokens on the project and the time:

```sql
SELECT s.step, s.phase, s.round, s.agent, SUM(u.output_tokens)
FROM workflow_steps s JOIN workflow_runs r USING (run_id)
JOIN turn_usage u ON u.project = r.project AND u.recorded_at BETWEEN s.at AND r.finished_at
GROUP BY s.run_id, s.step, s.phase, s.round;
```

What an agent said is not kept, as the audit log does not keep it.

A run is kept per project beside Halyard's database, so a restart picks it up where it
was, and there is one run per piece of work: a project has one working tree, and its
branch says which work that is. A run waiting on an agent goes on waiting: its card
closes saying the run is kept, and `/workflow` gives it a card again.

A step that reached nobody — Halyard paused, a session that could not be found, a
runtime that refused the message — stops the run under the chat's own warning. So does a
step whose turn started and then ended without an answer, out of usage halfway most
often: *develop's turn in xdrv ended without an answer*. So does `/stop` in the agent's
chat while the run waits on it: *develop's turn in xdrv was stopped by … before it
answered* — or, for a step still waiting behind the turn it stopped, *develop was stopped
by … before it reached xdrv*; a `/stop` after the answer came moves nothing. So does a restart that caught a
step on its way: its commands, its inspections and its message went with the process that
stopped. None of them counts a round, and the card's **▶️ develop → xdrv** sends the step
again.

A turn a restart took down after it started is not one of those. Halyard cannot tell it
from a turn still running, so the run goes on waiting for its answer. When the agent's
chat shows nothing came back, **⏹ Stop the workflow** clears the run, and
`/workflow level3 develop` sends that step afresh.
