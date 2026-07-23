"""What core expects from a runtime it can send a message to.

Kept to the one thing Phase 2 needs. The full adapter surface in the design
document — start, interrupt, event streams — is not written until something
needs it, because a protocol invented ahead of its second implementation
describes the first one wearing a disguise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionRef:
    """Where a session is, and what it is called.

    One shape for every runtime. The two that exist keep these facts in
    different places — Claude Code puts the name and the directory in one
    transcript, Codex splits the name into an index and the directory into a
    rollout — but what a channel needs to know is the same either way, and a
    channel that had to tell them apart would be a channel that knows about
    runtimes.
    """

    session_id: str
    name: str
    #: The directory the session belongs to. Both runtimes need it, for
    #: different reasons: Claude Code cannot find a conversation from anywhere
    #: else, and Codex applies the hooks of wherever it is run from.
    cwd: str | None
    model: str | None = None
    effort: str | None = None
    #: Whether a person chose the name. False means the runtime generated it,
    #: and a generated name is rewritten as the conversation moves — so a seat
    #: pointed at one works until it silently stops.
    named_by_a_person: bool = True
    started_at: datetime | None = None


@runtime_checkable
class AgentRunner(Protocol):
    """Delivers a message into an existing agent session."""

    @property
    def id(self) -> str:
        """Short identifier, used in audit records."""
        ...

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen here, as {name: (values, whether it is enforced)}.

        `session_id` is optional and a runtime may ignore it. Codex cannot:
        its effort levels depend on the model, with `ultra` on the two newest
        and `max` absent from the older ones — measured from the CLI's own
        catalog. Answering without knowing the session would offer a level
        that model then refuses, in the one place somebody looks to avoid
        being refused.

        Each runtime answers for itself. The alternative — a list of models kept
        in the channel — would have to be edited every time a runtime is added
        or a model ships, and would be wrong in a way nobody notices until a
        setting is silently ignored.

        An unenforced list is a hint: values outside it are still passed
        through. Say a set is enforced only when the runtime genuinely rejects
        everything else.
        """
        ...

    def resolve(self, name: str) -> SessionRef | None:
        """Find a session by the name a person gave it, or by its id.

        On the protocol because there are two implementations now and they
        disagree about where the answer lives. Before Codex this was a module
        function the channel imported directly from Claude Code, which is the
        arrangement that makes a second runtime impossible to add without
        editing the channel.
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
