"""A channel that answers by itself, for testing the path without a human.

This exists so the bridge and the endpoint can be exercised end to end before
Telegram is written. `StubChannel` in allow mode approves everything it is
shown, which makes it the single most dangerous object in the codebase if it is
ever running when somebody believes a human is being asked.

Three things guard against that, and none of them is a comment:

- `HALYARD_CHANNEL` has no default, so a stub cannot be selected by omission.
- Every decision is written to the audit log attributed to `stub:allow` or
  `stub:deny`, never to a person, so the log never implies a human was involved.
- A warning is logged on startup and on every single decision. It is meant to be
  annoying.
"""

from __future__ import annotations

import logging

from halyard.core.approvals import ApprovalRequest, ApprovalStore, Decision

logger = logging.getLogger(__name__)


class StubChannel:
    """Resolves every approval with a fixed decision, immediately."""

    def __init__(self, store: ApprovalStore, decision: Decision) -> None:
        self._store = store
        self._decision = decision
        self._sent = 0

    @property
    def name(self) -> str:
        return f"stub:{self._decision.value}"

    @property
    def decision(self) -> Decision:
        return self._decision

    @property
    def sent(self) -> int:
        """How many requests this channel has answered. Used by tests."""
        return self._sent

    @property
    def _past_tense(self) -> str:
        # This string is handed to the agent verbatim, so "Deny by the stub
        # channel" is not good enough.
        return "Allowed" if self._decision is Decision.ALLOW else "Denied"

    async def start(self) -> None:
        if self._decision is Decision.ALLOW:
            logger.warning(
                "StubChannel is APPROVING EVERY REQUEST WITHOUT ASKING ANYBODY. "
                "This is for testing the bridge only. Set HALYARD_CHANNEL=telegram "
                "for anything real."
            )
        else:
            logger.warning("StubChannel is denying every request without asking anybody.")

    async def stop(self) -> None:
        return None

    async def send_approval_request(self, request: ApprovalRequest) -> str:
        """Decide immediately, through the same path a human's answer takes.

        Going through `store.resolve()` with the real nonce rather than reaching
        past it keeps the stub honest: if the nonce or single-use rules were
        broken, the stub would break too instead of quietly routing around them.
        """
        self._sent += 1
        logger.warning(
            "StubChannel auto-%s: %s %s",
            self._decision.value,
            request.tool,
            request.command_summary,
        )
        await self._store.resolve(
            request.request_id,
            nonce=request.nonce,
            decision=self._decision,
            decided_by=self.name,
            note=f"{self._past_tense} by the stub channel. No human was asked.",
        )
        return f"stub-message-{self._sent}"

    async def send_message(self, session_id: str, text: str, role=None) -> str:
        logger.info("StubChannel message to %s: %s", session_id, text)
        return f"stub-message-{session_id}"

    async def send_long_content(self, session_id: str, content: str, title: str, role=None) -> str:
        logger.info(
            "StubChannel long content to %s: %s (%d chars)", session_id, title, len(content)
        )
        return f"stub-content-{session_id}"
