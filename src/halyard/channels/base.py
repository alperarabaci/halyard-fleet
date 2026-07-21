"""What core expects from anything that can reach a human.

A channel is not where decisions are made. It renders a request, collects an
answer, and hands that answer back to the approval store using the request's own
nonce. Everything about whether the answer is acceptable — is the nonce right,
has this already been decided, has it expired — stays in core, so a new channel
cannot get any of it subtly wrong.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from halyard.core.approvals import ApprovalRequest
from halyard.core.events import Role


@runtime_checkable
class ChannelAdapter(Protocol):
    """A place to send things, and a place answers come back from."""

    @property
    def name(self) -> str:
        """Short identifier, used in audit records."""
        ...

    async def start(self) -> None:
        """Open connections and begin listening for answers."""
        ...

    async def stop(self) -> None:
        """Shut down cleanly."""
        ...

    async def send_approval_request(self, request: ApprovalRequest) -> str:
        """Put an approval in front of a human. Returns a channel-side message id.

        Raising means the request did not reach anybody, which core treats as a
        denial: an approval nobody was asked about is not an approval.
        """
        ...

    async def send_message(self, session_id: str, text: str, role: Role | None = None) -> str:
        """Send plain text. Returns a channel-side message id.

        `role` is where the message belongs, for a channel that keeps a
        navigator and a driver apart. A channel with one destination ignores it.
        """
        ...

    async def send_long_content(
        self, session_id: str, content: str, title: str, role: Role | None = None
    ) -> str:
        """Send something too large for one message, however the channel prefers."""
        ...
