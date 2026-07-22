"""What core expects from a runtime it can send a message to.

Kept to the one thing Phase 2 needs. The full adapter surface in the design
document — start, interrupt, event streams — is not written until something
needs it, because a protocol invented ahead of its second implementation
describes the first one wearing a disguise.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentRunner(Protocol):
    """Delivers a message into an existing agent session."""

    @property
    def id(self) -> str:
        """Short identifier, used in audit records."""
        ...

    async def send(self, session_id: str, text: str) -> bool:
        """Put `text` into the session as if the user had typed it.

        Returns whether it was accepted. Must not raise: the caller is handling
        a chat message, and a failure to deliver is worth reporting rather than
        propagating.
        """
        ...
