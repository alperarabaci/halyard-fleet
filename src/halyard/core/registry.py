"""Which agent sessions the control plane currently knows about.

Sessions are not registered by an explicit handshake. In Phase 1 the first thing
Halyard ever hears from a session is a permission request arriving through the
hook bridge, so the registry is built around *observation*: a session comes into
existence the first time it asks for something, and every later request refreshes
it. A runtime that can announce itself properly is free to call `observe()`
earlier — the semantics are the same either way.

Phase 1 keeps this in memory. Sessions are scoped to a running Claude Code
process and do not outlive a control plane restart, so persisting them would
mostly mean reloading rows describing sessions that no longer exist. Phase 5
(state persistence) is where durable session identity is actually needed, and it
will want a schema shaped by handoff, not by this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from halyard.core.events import Role

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


class SessionStatus(StrEnum):
    """How live a session is, as far as the control plane can tell."""

    #: Seen recently, assumed to be running.
    ACTIVE = "active"
    #: Known, but nothing has been heard from it lately.
    IDLE = "idle"
    #: Explicitly finished. Kept so late callbacks can still be explained
    #: rather than silently failing against a missing session.
    ENDED = "ended"


class SessionInfo(BaseModel):
    """A snapshot of one agent session.

    Immutable, like `AgentEvent` — updates replace the entry rather than mutate
    it, so a snapshot handed to a caller cannot change underneath them while
    they are still deciding what to do with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_id: str
    project: str
    role: Role | None = None
    cwd: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    first_seen_at: datetime
    last_seen_at: datetime


class UnknownSessionError(KeyError):
    """Raised when an operation names a session the registry has never seen."""


class SessionRegistry:
    """An in-memory, async-safe registry of live agent sessions.

    Guarded by a lock because Claude Code dispatches independent tool calls in
    parallel: several hooks can block on the control plane at the same moment,
    each carrying the same `session_id`. Concurrent observations of one session
    must not race into two entries or lose the original `first_seen_at`.
    """

    def __init__(self, *, clock: Clock = _default_clock) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def observe(
        self,
        *,
        session_id: str,
        agent_id: str,
        project: str,
        role: Role | None = None,
        cwd: str | None = None,
    ) -> SessionInfo:
        """Record that a session was just heard from, creating it if needed.

        On a repeat sighting only `last_seen_at` and the status are refreshed,
        plus any field that arrived with a value where the stored one was empty.
        A later payload that omits `role` or `cwd` must not erase what an
        earlier, richer one established.
        """
        now = self._clock()
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                session = SessionInfo(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    role=role,
                    cwd=cwd,
                    status=SessionStatus.ACTIVE,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            else:
                session = existing.model_copy(
                    update={
                        "role": role if role is not None else existing.role,
                        "cwd": cwd if cwd is not None else existing.cwd,
                        "status": SessionStatus.ACTIVE,
                        "last_seen_at": now,
                    }
                )
            self._sessions[session_id] = session
            return session

    async def get(self, session_id: str) -> SessionInfo | None:
        """Return the session, or None if it was never observed."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_sessions(self) -> list[SessionInfo]:
        """Return every known session, oldest first."""
        async with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.first_seen_at)

    async def set_status(self, session_id: str, status: SessionStatus) -> SessionInfo:
        """Move a session to an explicit status.

        Raises `UnknownSessionError` rather than creating a placeholder: a
        status change for a session nobody has ever seen means a bug or a stale
        client, and inventing an entry would hide it.
        """
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                raise UnknownSessionError(session_id)
            session = existing.model_copy(update={"status": status, "last_seen_at": self._clock()})
            self._sessions[session_id] = session
            return session
