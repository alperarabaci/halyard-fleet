"""What a transition needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from halyard.commands import Command, Result
from halyard.inspections import Answer


@runtime_checkable
class Delivery(Protocol):
    """Where a transition ends: a seat's session, by the seat's label.

    The port a transition reaches a session through. The Telegram channel answers
    it with the path `/to` takes, so a transition lands exactly where a person
    would have sent it by hand, and says so in both chats.
    """

    async def to_seat(self, label: str, text: str) -> None: ...


@runtime_checkable
class Runner(Protocol):
    """How a transition runs one of its project's commands.

    The channel answers it: it knows where the project is, who is waiting to
    see it move, and what else is running there. A transition knows only which
    commands it names, and what came back.
    """

    async def run(self, command: Command) -> Result: ...


@dataclass(frozen=True)
class Handed:
    """What a transition did: the message it delivered, the commands it ran and
    the inspections that went with it."""

    text: str
    answers: tuple[Answer, ...] = ()
    ran: tuple[tuple[Command, Result], ...] = ()


@dataclass(frozen=True)
class Previous:
    """What the seat said back after the round before this one.

    Carried into every round after the first, so a reviewer asked again is shown
    what it found last time rather than set to find everything afresh. `text` is
    None when the seat has said nothing since that round reached it, which it is
    told rather than left to assume there was nothing to say.
    """

    #: The round it answered.
    number: int
    #: The seat, the way a sentence names it: `xrev (reviewer)`.
    seat: str
    #: When that round reached it, as the clock here reads.
    sent: str
    text: str | None = None
    #: When the answer came, as the clock here reads.
    at: str = ""
