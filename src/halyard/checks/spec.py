"""What a check needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Asker(Protocol):
    """A turn apart from any session: text in, an answer out, or None.

    The one port a check runs through. Claude Code's runner answers it today;
    any runtime that can take a turn of its own can, and that is where a check
    stops depending on one model.
    """

    async def ask(
        self, text: str, *, timeout: float = 180.0, model: str | None = None
    ) -> str | None: ...


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
