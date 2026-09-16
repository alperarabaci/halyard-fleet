"""What a workflow does next, given what the last seat decided.

One function, and no side effects: everything a run does is decided here and
carried out by whoever drives it. A flow that went round three times when it
should have gone round twice is the failure this exists to make testable.

**Forward is the default.** A decision Halyard could not read carries the work
to the next step rather than stopping — most handoffs ask for no decision at
all — and whoever drives this says so in the chat.

**Back is one step.** The step before is the one that produced what was just
judged: a navigator's back lands on the driver whose report it read. It is that
step's next round, so what the seat receives is the project's own text for
going round again.

**A step can act on the decision made before it.** A reviewer's back is for the
navigator to act on — the navigator holds the context, so the review goes to
it whichever way the reviewer decided, and the navigator's reply then goes
where the review said: back to the reviewer, or on. The step after the review
names it in `decided_by:`. A decision the navigator writes on its own last line
wins, and a reviewer's wait stops the run where it is, as any wait does.

**Rounds are what stop a loop.** A step may go as many times as its `rounds:`
allows, counting every time its handoff has gone for this piece of work — by
hand as well, since the seat read it either way. Past that the run stops and
says so: the next round is a person's to start, which is the whole lesson of
alpha-engine#361, where a review went round three times and nobody counted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from halyard.core.config_file import Step
from halyard.workflows.decisions import Decision, carried
from halyard.workflows.runs import Run


@dataclass(frozen=True)
class Next:
    """What the run does now: a step to take, a stop to say, or both.

    `stop` with a `step` is a step that is ready and did not go — the round it
    would be is past what the project allowed — so whoever drives this can
    offer to send it anyway.
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


def after(
    decision: Decision | None,
    *,
    run: Run,
    flow: Sequence[str],
    steps: Mapping[str, Step],
    taken: Mapping[str, int],
) -> Next:
    """Where the run goes after the seat at `run.step` decided.

    `taken` is how many rounds each handoff has already had for this piece of
    work, by its handoff's name.
    """
    here = steps.get(flow[run.step]) if 0 <= run.step < len(flow) else None
    deciding = decision
    if deciding is None and here is not None and here.decided_by:
        deciding = carried(run.carried)
    if deciding is Decision.WAIT:
        return Next(stop="it was asked to wait", decided=deciding)

    lending = lends(run.step, flow=flow, steps=steps)
    going_back = deciding is Decision.BACK and not lending
    target = run.step - 1 if going_back else run.step + 1
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
    would_be = taken.get(step.handoff, 0) + 1
    if would_be > step.rounds:
        return Next(
            step=target,
            back=going_back,
            carried=carries,
            decided=deciding,
            stop=f"{step.name} would go for round {would_be} of {step.rounds}",
        )
    return Next(step=target, back=going_back, carried=carries, decided=deciding)


def lends(index: int, *, flow: Sequence[str], steps: Mapping[str, Step]) -> bool:
    """Whether the step after this one acts on this one's decision — so this
    one's reply goes there, forward or back."""
    if not 0 <= index < len(flow) - 1:
        return False
    following = steps.get(flow[index + 1])
    return following is not None and following.decided_by == flow[index]
