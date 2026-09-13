"""What a handoff needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from halyard.checks import Answer


@runtime_checkable
class Delivery(Protocol):
    """Where a handoff ends: a seat's session, by the seat's label.

    The port a handoff reaches a session through. The Telegram channel answers
    it with the path `/to` takes, so a handoff lands exactly where a person
    would have sent it by hand, and says so in both chats.
    """

    async def to_seat(self, label: str, text: str) -> None: ...


@dataclass(frozen=True)
class Handed:
    """What a handoff did: the message it delivered, and the checks that went
    with it."""

    text: str
    answers: tuple[Answer, ...] = ()
