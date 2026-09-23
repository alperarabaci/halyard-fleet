"""What a workflow does next, given what the last seat decided.

One function, and no side effects: everything a run does is decided here and
carried out by whoever drives it. A flow that went round three times when it
should have gone round twice is the failure this exists to make testable.

**Forward is the default.** A decision Halyard could not read carries the work
to the next step rather than stopping — most handoffs ask for no decision at
all — and whoever drives this says so in the chat. The one exception is the end
of a phase, below.

**Back is one step.** The step before is the one that produced what was just
judged: a navigator's back lands on the driver whose report it read. It is that
step's next round, so what the seat receives is the project's own text for
going round again. From the step a later phase started at, the step before is
the end of the phase before it.

**A step can act on the decision made before it.** A reviewer's back is for the
navigator to act on — the navigator holds the context, so the review goes to
it whichever way the reviewer decided, and the navigator's reply then goes
where the review said: back to the reviewer, or on. The step after the review
names it in `decided_by:`. A decision the navigator writes on its own last line
wins, and a reviewer's wait stops the run where it is, as any wait does.

**Phases are the steps that go round again going forward.** A plan in two
parts is discovered, developed and verified twice before the work closes, and
no number of rounds says that: a round is the same step again because it was
not right. So a flow can hold one list of steps inside it, and the last of them
decides between `next` — another phase, from the first of them or from the one
it names — and `forward`, out of the phases to whatever comes after. Only
there: a `next` anywhere else stops the run, and so does a phase that ends
with no decision at all, because carrying on would leave the phases with
nobody having said the work was done. A `wait` there stops with the same two
ways on ready, since what the operator does between parts is apply the one
just made, and what follows is the next part or the end of them.

**Rounds and phases are what stop a loop.** A step may go as many times as its
`rounds:` allows in one run — in each phase, inside the phases — and the phases
as many times as `phases:` allows. Past either the run stops and says so: the
next round is a person's to start, which is the whole lesson of
alpha-engine#361, where a review went round three times and nobody counted.
Every stop leaves the run where it was, for somebody to send on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from halyard.core.config_file import Step
from halyard.workflows.decisions import Decision, carried
from halyard.workflows.runs import Run, counted_as

#: How many phases a flow goes through unasked when nobody configured it.
PHASES = 3


@dataclass(frozen=True)
class Next:
    """What the run does now: a step to take, a stop to say, or both.

    `stop` with a `step` is a step that is ready and did not go — the round or
    the phase it would be is past what the project allowed, or nobody decided
    — so whoever drives this can offer to send it anyway.
    """

    step: int | None = None
    #: The step is a return to an earlier one, which the seat is told.
    back: bool = False
    #: The flow reached the end of its steps.
    done: bool = False
    #: Why it is not going on, in a sentence for the chat.
    stop: str = ""
    #: The decision the step taken is to act on: the one just made, when that
    #: step names the one that made it in `decided_by:`.
    carried: Decision | None = None
    #: What this was decided on — the reply's own decision, or the one carried
    #: to its step. None when there was neither.
    decided: Decision | None = None
    #: The phase the step taken is in, when that is not the run's own.
    phase: int | None = None
    #: Where the step taken starts a phase, when it does.
    entered: int | None = None
    #: The steps a `next` that named where to start went past.
    skipped: tuple[str, ...] = ()
    #: For a stop at the end of a phase: where `forward` out of the phases goes
    #: — the length of the flow when nothing comes after them.
    leaving: int | None = None


def after(
    decision: Decision | None,
    *,
    run: Run,
    flow: Sequence[str],
    steps: Mapping[str, Step],
    taken: Mapping[str, int],
    stretch: tuple[int, int] | None = None,
    most: int = PHASES,
    named: str = "",
) -> Next:
    """Where the run goes after the seat at `run.step` decided.

    `taken` is how many rounds each step has already had in this run, by what
    it is counted as — see `runs.counted_as`. `stretch` is the first and last
    place of the flow's phases, if it has any, and `most` how many phases it
    may go through. `named` is the step a `next` named, if it named one.
    """
    here = steps.get(flow[run.step]) if 0 <= run.step < len(flow) else None
    deciding = decision
    if deciding is None and here is not None and here.decided_by:
        deciding = carried(run.carried)
    ending = stretch is not None and run.step == stretch[1]
    lending = lends(run.step, flow=flow, steps=steps)
    if deciding is Decision.WAIT and ending and not lending:
        # A wait at the end of a phase is the operator's moment between parts —
        # applying what was made, before the next one starts on it — and what
        # comes after it is the next phase or the way out of them, not the step
        # that happens to follow in the list.
        ready = _next_phase(run, flow=flow, steps=steps, taken=taken, stretch=stretch, most=most)
        assert stretch is not None  # `ending` said so
        return replace(ready, stop="it was asked to wait", decided=deciding, leaving=stretch[1] + 1)
    if deciding is Decision.WAIT:
        return Next(stop="it was asked to wait", decided=deciding)
    if deciding is Decision.NEXT:
        return _next_phase(
            run, flow=flow, steps=steps, taken=taken, stretch=stretch, most=most, named=named
        )
    if deciding is None and ending and not lending:
        # Leaving the phases is forward, and so is carrying on; a phase that
        # ended without a word would leave them without anybody deciding to.
        # The next phase is made ready, and the chat offers it or the way out.
        ready = _next_phase(run, flow=flow, steps=steps, taken=taken, stretch=stretch, most=most)
        assert stretch is not None  # `ending` said so
        return replace(
            ready,
            stop=f"phase {run.phase} ended with no decision",
            decided=None,
            leaving=stretch[1] + 1,
        )

    going_back = deciding is Decision.BACK and not lending
    phase, entered = run.phase, None
    if going_back and stretch is not None and run.phase > 1 and run.step == run.entered:
        # The first step of a later phase was handed the end of the one before.
        # Where that one started is not kept, and the phases' first step is
        # where a phase starts unless a `next` said otherwise.
        target, phase, entered = stretch[1], run.phase - 1, stretch[0]
    else:
        target = run.step - 1 if going_back else run.step + 1
        if stretch is not None and not going_back and target == stretch[0]:
            entered = target
    if going_back and target < 0:
        return Next(
            stop="it was sent back from its first step, which has nothing before it",
            decided=deciding,
        )
    if target >= len(flow):
        return Next(done=True, decided=deciding)

    step = steps.get(flow[target])
    if step is None:
        # A flow naming a step nobody defined is refused when the file is read,
        # so this is a run written by an older configuration than the one
        # loaded now. Stopping says which name, where guessing would deliver
        # somebody else's step.
        return Next(
            stop=f"its next step, {flow[target]}, is not one this project defines",
            decided=deciding,
        )

    carries = deciding if lending else None
    would_be = taken.get(counted_as(step.name, target, phase=phase, stretch=stretch), 0) + 1
    going = Next(
        step=target,
        back=going_back,
        carried=carries,
        decided=deciding,
        phase=phase if phase != run.phase else None,
        entered=entered,
    )
    if would_be > step.rounds:
        return replace(going, stop=f"{step.name} would go for round {would_be} of {step.rounds}")
    return going


def _next_phase(
    run: Run,
    *,
    flow: Sequence[str],
    steps: Mapping[str, Step],
    taken: Mapping[str, int],
    stretch: tuple[int, int] | None,
    most: int,
    named: str = "",
) -> Next:
    """The next phase, from the first of the phases' steps or the one named.

    Refused anywhere but the last step of the phases: a `next` in the middle of
    one is a seat that lost its place, and guessing which phase it meant would
    be the run losing its place too.
    """
    said = Decision.NEXT
    if stretch is None:
        return Next(stop="it said next, and this workflow has no phases", decided=said)
    first, last = stretch
    if run.step != last:
        return Next(
            stop=f"it said next at {flow[run.step]}, which is not where a phase ends",
            decided=said,
        )
    target = first
    if named:
        places = [place for place in range(first, last + 1) if flow[place] == named]
        if not places:
            return Next(
                stop=f"it said next {named}, which is not one of this workflow's phase steps",
                decided=said,
            )
        target = places[0]
    step = steps.get(flow[target])
    if step is None:
        return Next(
            stop=f"its next step, {flow[target]}, is not one this project defines", decided=said
        )
    phase = run.phase + 1
    going = Next(
        step=target,
        decided=said,
        phase=phase,
        entered=target,
        skipped=tuple(flow[first:target]),
    )
    would_be = taken.get(counted_as(step.name, target, phase=phase, stretch=stretch), 0) + 1
    if phase > most:
        return replace(
            going, stop=f"phase {phase} would be past the {most} allowed", leaving=last + 1
        )
    if would_be > step.rounds:
        return replace(
            going,
            stop=f"{step.name} would go for round {would_be} of {step.rounds}",
            leaving=last + 1,
        )
    return going


def lends(index: int, *, flow: Sequence[str], steps: Mapping[str, Step]) -> bool:
    """Whether the step after this one acts on this one's decision — so this
    one's reply goes there, forward or back."""
    if not 0 <= index < len(flow) - 1:
        return False
    following = steps.get(flow[index + 1])
    return following is not None and following.decided_by == flow[index]
