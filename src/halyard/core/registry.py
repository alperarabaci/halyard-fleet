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
will want a schema shaped by how sessions change hands, not by this.

**Some sessions are Halyard's own.** An inspection, or one run again, is a turn
Halyard starts in a session of its own — no agent's, working on no task. Its
hooks and plugins report it like any other, so it is marked here before its
turn begins (`mark_own`), and what hears from it asks (`own`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from halyard.core.events import Role

Clock = Callable[[], datetime]

logger = logging.getLogger(__name__)


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
    #: The name the runtime knows the session by, when the hook carried one —
    #: what a seat in `halyard.yaml` is matched on. See `seats.for_session`.
    session_name: str | None = None
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

    #: How long a session stays marked as Halyard's own. Longer than any turn
    #: Halyard starts for itself, so its last reply is still recognised.
    OWN_FOR = timedelta(hours=1)

    def __init__(self, *, clock: Clock = _default_clock) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        #: Told about every sighting. See `listen`.
        self._listeners: list[Callable[[SessionInfo], None]] = []
        #: Halyard's own sessions: what each turn is — `proof · repeat` — and
        #: when it was marked.
        self._own: dict[str, tuple[str, datetime]] = {}

    def mark_own(self, session_id: str, label: str) -> None:
        """A session Halyard opened for a turn of its own, and what that turn is.

        Not an agent's: what it says is kept out of the chat, it is seen working
        on no task, and a command it asks to run is shown as `label`'s.
        """
        if session_id:
            self._own[session_id] = (label or "Halyard", self._clock())

    def own(self, session_id: str | None) -> str | None:
        """What turn of Halyard's own this session is, or None when it is an
        agent's — or was Halyard's too long ago to still say."""
        found = self._own.get(session_id or "")
        if found is None:
            return None
        label, marked = found
        if self._clock() - marked > self.OWN_FOR:
            self._own.pop(session_id or "", None)
            return None
        return label

    async def observe(
        self,
        *,
        session_id: str,
        agent_id: str,
        project: str,
        role: Role | None = None,
        session_name: str | None = None,
        cwd: str | None = None,
    ) -> SessionInfo:
        """Record that a session was just heard from, creating it if needed.

        On a repeat sighting only `last_seen_at` and the status are refreshed,
        plus any field that arrived with a value where the stored one was empty.
        A later payload that omits `role`, `session_name` or `cwd` must not
        erase what an earlier, richer one established.
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
                    session_name=session_name,
                    cwd=cwd,
                    status=SessionStatus.ACTIVE,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            else:
                session = existing.model_copy(
                    update={
                        "role": role if role is not None else existing.role,
                        "session_name": (
                            session_name if session_name is not None else existing.session_name
                        ),
                        "cwd": cwd if cwd is not None else existing.cwd,
                        "status": SessionStatus.ACTIVE,
                        "last_seen_at": now,
                    }
                )
            self._sessions[session_id] = session
        # Outside the lock, and never allowed to raise: whatever listens is
        # told on the path of an approval, and a listener that fails must not
        # become an approval that fails.
        for listener in self._listeners:
            try:
                listener(session)
            except Exception:
                logger.exception("A session listener failed")
        return session

    def listen(self, listener: Callable[[SessionInfo], None]) -> None:
        """Be told each time a session is heard from.

        Called with the session as it now stands, after the registry has let
        go of its lock. A listener must return at once — this runs on the path
        of every approval — so anything slow belongs in a task of its own.
        The registry does not know what listens; that is the point of it.
        """
        self._listeners.append(listener)

    async def get(self, session_id: str) -> SessionInfo | None:
        """Return the session, or None if it was never observed."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_sessions(self) -> list[SessionInfo]:
        """Return every known session, oldest first."""
        async with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.first_seen_at)

    async def latest(self) -> SessionInfo | None:
        """The most recently seen session, whatever seat it is in.

        For the ordinary setup with one session and one chat, where asking
        which seat a message belongs to would be asking about a distinction
        that does not exist.
        """
        async with self._lock:
            candidates = [s for s in self._sessions.values() if s.status is not SessionStatus.ENDED]
        return max(candidates, key=lambda s: s.last_seen_at, default=None)

    async def latest_for_role(self, role: Role) -> SessionInfo | None:
        """The most recently seen session sitting in a given seat.

        This is how a message typed in a chat finds the session it belongs to.
        Most recent rather than any, because a named conversation is resumed
        under a new id each time it restarts, and the one heard from last is the
        one still running.
        """
        async with self._lock:
            candidates = [
                session
                for session in self._sessions.values()
                if session.role is role and session.status is not SessionStatus.ENDED
            ]
        return max(candidates, key=lambda s: s.last_seen_at, default=None)

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
