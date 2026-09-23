"""What a workflow adds to the envelope of each step it takes.

A handoff pressed by hand carries nothing of a workflow, because there is none.
These lines are the workflow's own, put on the envelope by the step it takes:
where the seat is in the flow, and where each word on its last line would take
the work — which step, which seat, which round, which phase — worked out by the
same `after` that moves the run when the reply comes, so what a seat is told
and what happens cannot disagree.

One template for every step of every project, in the words that project
decides in. The workflow knows all of it, and a project's prompts only have to
ask for the decision line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from halyard.core.config_file import Decisions, Step
from halyard.workflows.decisions import Decision, carried, word_for
from halyard.workflows.flow import PHASES, after, lends
from halyard.workflows.runs import Run, counted_as

#: The decisions every step can make; `next` is only offered where a phase ends.
_EVERYWHERE = (Decision.FORWARD, Decision.BACK, Decision.WAIT)


def lines_for(
    run: Run,
    *,
    flow: Sequence[str],
    steps: Mapping[str, Step],
    taken: Mapping[str, int],
    seats: Mapping[str, str],
    words: Decisions | None = None,
    sent_back_by: str = "",
    stretch: tuple[int, int] | None = None,
    most: int = PHASES,
) -> list[str]:
    """The workflow's lines for the step `run` is on, one fact to a line.

    `taken` is how many rounds each step has had in this run, by what it is
    counted as, this step's own included, since it is on its way. `seats` is
    the label of the seat each step goes to, by step name. `words` are the
    project's words for each decision. `sent_back_by` names who sent the work
    back to this step, and when, if it came back. `stretch` and `most` are the
    flow's phases, if it has any.
    """
    name = flow[run.step] if 0 <= run.step < len(flow) else ""
    said = [f"Workflow: {run.workflow} · step {run.step + 1} of {len(flow)} · {name}"]
    inside = stretch is not None and stretch[0] <= run.step <= stretch[1]
    if inside:
        said.append(f"Phase: {run.phase}")
    if sent_back_by:
        said.append(f"Sent back by: {sent_back_by}")
    step = steps.get(name)
    if step is None:
        return said

    def to(decision: Decision) -> str:
        return _where(
            decision,
            run=run,
            flow=flow,
            steps=steps,
            taken=taken,
            seats=seats,
            stretch=stretch,
            most=most,
        )

    def word(decision: Decision) -> str:
        return word_for(decision, words)

    if lends(run.step, flow=flow, steps=steps):
        said.append(
            f"Decide on your last line: {word(Decision.FORWARD)} or {word(Decision.BACK)} "
            f"({to(Decision.FORWARD)}, who acts on it) · "
            f"{word(Decision.WAIT)} ({to(Decision.WAIT)})"
        )
        return said
    already = carried(run.carried) if step.decided_by else None
    if already is not None:
        by = step.decided_by or ""
        who = f" ({seats[by]})" if seats.get(by) else ""
        others = [decision for decision in _EVERYWHERE if decision is not already]
        said.append(f"Already decided by {by}{who}: {word(already)} ({to(already)})")
        said.append(
            "To overrule it, decide on your last line: "
            + " or ".join(f"{word(decision)} ({to(decision)})" for decision in others)
        )
        return said
    said.append(
        "Decide on your last line: "
        + " · ".join(f"{word(decision)} ({to(decision)})" for decision in _EVERYWHERE)
    )
    if stretch is not None and run.step == stretch[1]:
        # Only here: the end of a phase is the one place `next` means anything,
        # and the one place a reply with no decision stops rather than going on.
        phases = flow[stretch[0] : stretch[1] + 1]
        said.append(
            f"This step ends phase {run.phase}: {word(Decision.NEXT)} ({to(Decision.NEXT)}) "
            f"starts the next one, and {word(Decision.NEXT)} <step> starts it at another of "
            f"{', '.join(phases)}; {word(Decision.FORWARD)} leaves the phases. "
            "A reply with no decision waits for the operator."
        )
    return said


def _where(
    decision: Decision,
    *,
    run: Run,
    flow: Sequence[str],
    steps: Mapping[str, Step],
    taken: Mapping[str, int],
    seats: Mapping[str, str],
    stretch: tuple[int, int] | None,
    most: int,
) -> str:
    """Where a decision takes the work from this step: `→ discover, xdrv, round 2 of 2`."""
    going = after(
        decision, run=run, flow=flow, steps=steps, taken=taken, stretch=stretch, most=most
    )
    if going.done:
        return "→ the workflow ends"
    if going.step is None:
        return "→ the operator"
    name = flow[going.step]
    target = steps[name]
    phase = going.phase or run.phase
    parts = [name, *([seats[name]] if seats.get(name) else [])]
    if going.phase is not None and going.phase > run.phase:
        parts.append(f"phase {going.phase}")
    would_be = taken.get(counted_as(name, going.step, phase=phase, stretch=stretch), 0) + 1
    if would_be > 1:
        parts.append(f"round {would_be} of {target.rounds}")
    if going.stop:
        # Past its rounds or its phases: the step waits for somebody to send it.
        parts.append("which waits for the operator")
    return "→ " + ", ".join(parts)
