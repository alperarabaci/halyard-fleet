"""Which piece of work a round belongs to, and how a round is shown.

A reply sent back comes round again, and nothing said so. alpha-engine#361's
review went three times on 15 September, each time with the whole review text
in front as though it were the first — and the reviewer, told nothing of what it
had found before, found something new each time until somebody cut it off. So a
workflow's step counts its rounds: the envelope says `Round: 2/2`, and a
transition with a `followup_prompt:` sends that from the second round on, with the
answer the seat gave to the round before.

**Rounds are a workflow's.** They are counted by the run, per step — and per
phase inside a flow's phases — in `halyard.workflows.runs`. A transition pressed by
hand counts none and says none: it sends its own prompt every time, and a `1/2`
on something that has no second step would be a workflow's word where there is
no workflow.
"""

from __future__ import annotations

from halyard.tasks.branches import number_of


def work_of(branch: str | None, project: str) -> str | None:
    """What rounds and runs are counted against: the work item the branch
    names — `alpha-engine#361` — or the branch itself when its name has no
    number.

    None on a detached head, where there is nothing to count against.
    """
    if not branch:
        return None
    number = number_of(branch)
    return f"{project}#{number}" if number is not None else branch


def shown(number: int, expected: int | None = None) -> str:
    """A round the way the envelope and the chat say it: `2/2`, or `2` where
    nothing says how many there may be."""
    return f"{number}/{expected}" if expected else str(number)
