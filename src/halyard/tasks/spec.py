"""What a forge has to be able to answer.

`Forge` is to issue trackers what `RuntimeSpec` is to agents: the one place
that says what differs, so that adding GitHub is adding a module rather than
finding every branch on a provider name. Nothing outside this package should
learn what a GitLab is.

Three questions and no more, because that is all `/label` asks. A forge that
could also close issues, or comment, or open merge requests would be a nicer
abstraction and a worse one — every method here is one somebody has to
implement before their provider works at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ForgeError(RuntimeError):
    """The forge refused, and its own words are the best explanation."""


@dataclass(frozen=True)
class Task:
    """One issue, as much of it as a phone needs."""

    number: int
    title: str
    #: What is already on it, so the same label is never offered twice.
    labels: tuple[str, ...] = ()
    url: str = ""


@runtime_checkable
class Forge(Protocol):
    """One project on one issue tracker."""

    #: How this is spelled in a message. Not the host: a self-hosted GitLab is
    #: still GitLab, and `git.example.com` would tell nobody anything.
    name: str

    async def task(self, number: int) -> Task:
        """The issue, or `ForgeError` if it cannot be read."""
        ...

    async def labels(self) -> tuple[str, ...]:
        """Every label this project defines."""
        ...

    async def add_label(self, number: int, label: str) -> Task:
        """Put one more label on the issue, leaving the rest alone."""
        ...
