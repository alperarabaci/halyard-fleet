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

    def options(self) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen here, as {name: (values, whether it is enforced)}.

        Each runtime answers for itself. The alternative — a list of models kept
        in the channel — would have to be edited every time a runtime is added
        or a model ships, and would be wrong in a way nobody notices until a
        setting is silently ignored.

        An unenforced list is a hint: values outside it are still passed
        through. Say a set is enforced only when the runtime genuinely rejects
        everything else.
        """
        ...

    def busy(self, session_id: str) -> bool:
        """Whether this runner is already mid-turn in that session.

        Only what it started itself. A turn somebody began at a keyboard is
        invisible from here, and claiming otherwise would be worse than silence.
        """
        ...

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        """The model and effort this runner will use for that session, if chosen."""
        ...

    def set_model(self, session_id: str, model: str | None) -> None:
        """Choose the model for turns this runner starts. None gives it back."""
        ...

    def set_effort(self, session_id: str, effort: str | None) -> None:
        """Choose the reasoning effort. None gives it back."""
        ...

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        """Put `text` into the session as if the user had typed it.

        `cwd` is the directory the session belongs to, for runtimes that scope
        a session to a project.

        Returns whether it was accepted. Must not raise: the caller is handling
        a chat message, and a failure to deliver is worth reporting rather than
        propagating.
        """
        ...
