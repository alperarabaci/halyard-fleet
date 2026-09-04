"""Which labels are worth offering, and putting one on.

The rule, kept out of the channel. Deciding what to show is not rendering, and
it went into the Telegram adapter first — where it sat between two f-strings
full of HTML, testable only by pretending to be a phone.

Two rules, both about not wasting a tap:

- A label already on the task is never offered. That also means the "it is
  already there" case cannot arise, so nothing has to handle it.
- A project may narrow the list. Empty means every label the project defines,
  which is right until a project has more of them than a phone can show.

Neither rule is GitLab's. They hold for whatever answers `Forge`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from halyard.tasks.spec import Forge, Task


@dataclass(frozen=True)
class Choice:
    """A task, and what could still be put on it."""

    task: Task
    #: In the order the forge defines them, minus what is already there.
    offer: tuple[str, ...] = ()

    @property
    def anything_left(self) -> bool:
        return bool(self.offer)


def worth_offering(
    task: Task, defined: Sequence[str], narrow: Sequence[str] = ()
) -> tuple[str, ...]:
    """The labels that would change something if tapped.

    Case-insensitive about what is already there, because a forge lets
    `Andon` and `andon` be the same label to a person and different strings to
    a comparison.
    """
    already = {name.lower() for name in task.labels}
    wanted = {name.lower() for name in narrow}
    return tuple(
        name
        for name in defined
        if name.lower() not in already and (not wanted or name.lower() in wanted)
    )


async def to_offer(forge: Forge, number: int, narrow: Sequence[str] = ()) -> Choice:
    """Read the task and work out what could go on it.

    Two calls rather than one, because a forge answers "what is on this issue"
    and "what labels exist here" separately, and both are needed to say what
    would change anything.
    """
    task = await forge.task(number)
    return Choice(task=task, offer=worth_offering(task, await forge.labels(), narrow))


async def put_on(forge: Forge, number: int, label: str) -> Task:
    """Add one label, and return the task as it now stands."""
    return await forge.add_label(number, label.strip())
