"""What a check needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Asker(Protocol):
    """A turn apart from any session: text in, an answer out, or None.

    The one port a check runs through. The channel answers it today, with
    Claude Code's runner behind it; any runtime that can take a turn of its own
    can, and that is where a check stops depending on one model.

    `cwd` is where the turn runs. `name` is what it goes by wherever it is shown
    to a person — a command it asks to run is a card, and the card has to say
    whose. `edits=False` is a turn with no tool that edits a file: it may still
    read, and run commands, which the project's gate puts in front of somebody.
    """

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        name: str | None = None,
        edits: bool = True,
    ) -> str | None: ...


@runtime_checkable
class Labeller(Protocol):
    """What a check may do to the task it ran on: put one more label on it.

    The channel answers it — it knows which task, which tracker and which
    token. A check knows only what it found, and never waits on the writing.
    """

    async def label(self, label: str) -> None: ...


class StoppedError(Exception):
    """A check's turn ended by a person before it answered.

    Raised by whatever runs the turn, and kept as the reason on an unmeasured
    answer: a check somebody stopped says so, rather than reading as a model
    that went quiet.
    """


@dataclass(frozen=True)
class Answer:
    """One check's answer — or why there is none, which is an answer too."""

    name: str
    #: The check's own file, and the commit that last changed it.
    path: Path
    version: str
    #: What the model said, with a wrapping code fence taken off. None when the
    #: check did not run or the model did not answer.
    text: str | None = None
    #: Why there is no text, when there is none.
    why: str = ""
    #: How long the model took, in seconds.
    took: float = 0.0

    @property
    def measured(self) -> bool:
        """Whether there is anything to read. An unmeasured check is never a
        clean one."""
        return self.text is not None
