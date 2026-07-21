"""Whether approvals are being relayed to a channel at all.

There has to be a way to stop being asked that does not involve walking to the
machine. You sit down at the keyboard and no longer need your phone; you go into
a meeting and do not want forty cards; something gets stuck. Without a switch,
the only recovery is editing settings.json and restarting a session.

**Pausing is not approving.** When the gate is closed, Halyard does not decide —
it declines to answer, and Claude Code falls back to the permission prompt it
would have shown if Halyard had never been installed. The question moves back to
the terminal rather than disappearing. Automatic approval is on this project's
list of things it will not do, and a pause switch that granted commands would be
exactly that with a friendlier name.

The state lives in memory. A control plane that restarts comes back asking,
because between "resumed without being told" and "silently stopped asking", the
first is the one you notice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


class GateState(BaseModel):
    """Whether the gate is open, and who last touched it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paused: bool
    changed_at: datetime
    #: Channel-scoped identity of whoever last changed it, or None if nobody
    #: has — a gate that has never been touched was not opened by anyone.
    changed_by: str | None = None


class Gate:
    """The switch, and a record of who last threw it."""

    def __init__(self, *, clock: Clock = _default_clock) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state = GateState(paused=False, changed_at=clock())

    @property
    def paused(self) -> bool:
        """Read without waiting. Approvals check this on every request."""
        return self._state.paused

    async def state(self) -> GateState:
        async with self._lock:
            return self._state

    async def pause(self, by: str | None = None) -> tuple[GateState, bool]:
        """Stop relaying. Returns the state and whether this call changed it.

        Idempotent, and it reports which it was. Pausing something already
        paused should confirm rather than complain — the point of the switch is
        to be reachable when things are going wrong, and an error at that moment
        reads as a failure.
        """
        return await self._set(True, by)

    async def resume(self, by: str | None = None) -> tuple[GateState, bool]:
        """Start relaying again. Idempotent, like `pause`."""
        return await self._set(False, by)

    async def _set(self, paused: bool, by: str | None) -> tuple[GateState, bool]:
        async with self._lock:
            if self._state.paused == paused:
                return self._state, False
            self._state = GateState(paused=paused, changed_at=self._clock(), changed_by=by)
            return self._state, True
