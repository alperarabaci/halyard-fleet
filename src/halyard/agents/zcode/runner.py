"""What Halyard can do to a ZCode session from outside the application.

Not send to it, yet. Approvals and replies need nothing from here — the hooks
carry both. Putting a message into a session does, and nothing outside ZCode is
known to: no resume-and-send mode has been measured in its engine, and the
application keeps its sessions to itself. So this says as much every time it is
asked, rather than reporting a message as sent that went nowhere.
"""

from __future__ import annotations

import logging

from halyard.agents.base import SessionRef
from halyard.agents.turns import LateFailure
from halyard.agents.zcode import trust

logger = logging.getLogger(__name__)


class ZCodeRunner:
    """A runner that can describe ZCode and cannot deliver to it."""

    @property
    def id(self) -> str:
        return "zcode"

    @property
    def available(self) -> bool:
        return trust.app() is not None

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        return {}

    def resolve(self, name: str) -> SessionRef | None:
        return None

    def busy(self, session_id: str) -> bool:
        return False

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        return (None, None)

    def set_model(self, session_id: str, model: str | None) -> None:
        return None

    def set_effort(self, session_id: str, effort: str | None) -> None:
        return None

    async def send(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        when_done: LateFailure | None = None,
    ) -> bool:
        logger.warning(
            "Not delivered to ZCode session %s: nothing outside the application can put "
            "a message into one yet, so it has to be typed at the desk",
            session_id,
        )
        return False
