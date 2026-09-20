"""Putting a message into a ZCode session, and holding its gate while it runs.

ZCode keeps its sessions to itself: no port, no send command, and a headless
run cannot be given a permission client at all — measured, twice. What it does
have is the protocol its own application drives it with, where whoever starts
the engine answers for it. So Halyard starts one and answers: the credential
for the model call, from the token in `halyard.yaml`, and every side-effect
tool, from the gate that already answers for every other runtime.

**The session is told to ask.** A session in "Edit automatically" writes files
without asking anybody, and so does one whose stored mode says otherwise until
the mode is set again after it opens. So it is set — every time, after opening,
before sending. A seat whose desk mode was "Edit automatically" is asking after
Halyard has sent to it, which is the safe direction to be wrong in.

**The bridge lives as long as the turn.** The questions arrive throughout it,
not at the start, and an engine that is closed early ends the turn with them.
So `send` returns as soon as the message is accepted — that is what delivery
means everywhere else in Halyard — and the bridge stays up behind it until the
turn ends, says what it ended as, and closes.

**A gate that says nothing is a gate that hangs the seat.** An unanswered
question is repeated every few seconds and the turn never finishes, so an
expired card is answered `deny` rather than left. Nothing here decides that;
`Asking` does, and it is given from outside.
"""

from __future__ import annotations

import asyncio
import logging

from halyard.agents.base import SessionRef
from halyard.agents.turns import LateFailure
from halyard.agents.zcode import account, sessions, trust
from halyard.agents.zcode.protocol import ASKING, Answer, Asking, Bridge, Permission

logger = logging.getLogger(__name__)

#: What a turn may take before Halyard stops holding its gate. Long, because a
#: driver working through a report is measured in tens of minutes, and bounded,
#: because an engine nobody closed keeps a session open for ever.
TURN_SECONDS = 4 * 60 * 60.0

#: What the model selection needs beside the model itself. ZCode refuses a send
#: without one — "Reasoning level is required" — so it always carries one.
REASONING = "max"

#: How a model is written in configuration, the way ZCode writes it itself:
#: `account:zai-individual-coding-plan/GLM-5.3-Flash`.
SEPARATOR = "/"


class ZCodeRunner:
    """A seat in ZCode, reachable from the phone."""

    def __init__(
        self,
        *,
        token: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        asking: Asking | None = None,
    ) -> None:
        self._token = (token or "").strip()
        self._model = (model or "").strip()
        self._reasoning = (reasoning or REASONING).strip() or REASONING
        self._asking = asking
        #: The sessions a turn is running in, so a second message waits rather
        #: than starting a turn on top of one.
        self._running: dict[str, Bridge] = {}
        self._turns: set[asyncio.Task] = set()

    @property
    def id(self) -> str:
        return "zcode"

    @property
    def available(self) -> bool:
        return trust.app() is not None

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        return {}

    def resolve(self, name: str) -> SessionRef | None:
        """The session a seat names, from ZCode's own list of them."""
        return sessions.find_session(name)

    def busy(self, session_id: str) -> bool:
        return session_id in self._running

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        return (self._model or None, self._reasoning)

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
        """Put one message into a session, and hold its gate while it answers."""
        if not session_id or not (text or "").strip():
            return False
        if (missing := self._unready()) is not None:
            logger.warning("Not delivered to ZCode session %s: %s", session_id, missing)
            return False
        if self.busy(session_id):
            logger.info("ZCode session %s is still answering; not sending on top of it", session_id)
            return False

        provider, _, model = self._model.partition(SEPARATOR)
        shown = account.read(provider)
        if shown is None:
            logger.warning(
                "Not delivered to ZCode session %s: its provider catalog could not be read",
                session_id,
            )
            return False

        # What the card will say this seat is: ZCode's ids are opaque, and the
        # title is what a seat is configured by.
        known = sessions.find_session(session_id)
        name = known.name if known is not None else ""
        cwd = cwd or (known.cwd if known is not None else None)

        found = trust.app()
        assert found is not None  # `_unready` said so
        bridge = Bridge(
            [str(found / trust.EXECUTABLE), str(found / trust.ENGINE), "app-server", "--stdio"],
            cwd=cwd,
            env={
                "ELECTRON_RUN_AS_NODE": "1",
                "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(shown.catalog),
            },
            token=self._token,
            asking=self._asking or _refuse,
            about={"cwd": cwd or "", "session_name": name or ""},
        )
        try:
            accepted = await self._open(bridge, shown, session_id, text, provider, model)
        except (OSError, RuntimeError, TimeoutError, ValueError) as failed:
            logger.warning("The ZCode bridge failed for session %s: %s", session_id, failed)
            await bridge.close()
            return False
        if not accepted:
            await bridge.close()
            return False

        self._running[session_id] = bridge
        # Detached on purpose: delivery is done, and what is left is the turn.
        # Held in a set as well, so nothing collects it half way through.
        turn = asyncio.create_task(self._hold(session_id, bridge, when_done), name="zcode-turn")
        self._turns.add(turn)
        turn.add_done_callback(self._turns.discard)
        return True

    def _unready(self) -> str | None:
        """Why this could not send at all, in a sentence for the log."""
        if trust.app() is None:
            return "ZCode is not installed here"
        if not self._token:
            return "no ZCODE_TOKEN in halyard.yaml, and the engine will not run a turn without one"
        if SEPARATOR not in self._model:
            return (
                "no ZCODE_MODEL in halyard.yaml — write it as ZCode does, `account:<plan>/<model>`"
            )
        return None

    async def _open(
        self,
        bridge: Bridge,
        shown: account.Account,
        session_id: str,
        text: str,
        provider: str,
        model: str,
    ) -> bool:
        """Open the session the way the desktop does, and send the message."""
        await bridge.start()
        await bridge.call("provider/updateAccountConfig", shown.snapshot())
        workspace = await self._workspace(bridge, session_id)
        if workspace is None:
            logger.warning("ZCode does not have a session %s to resume", session_id)
            return False
        await bridge.call("session/resume", {"sessionId": session_id, "workspace": workspace})
        # After opening, not before: a session whose stored mode already asks
        # still does not enforce until this call. Measured.
        await bridge.call("session/setMode", {"sessionId": session_id, "mode": ASKING})
        await bridge.call(
            "session/subscribe", {"sessionId": session_id, "deliveryKind": "desktop-continuous"}
        )
        sent = await bridge.call(
            "session/send",
            {
                "sessionId": session_id,
                "content": text,
                "modelSelection": {
                    "providerId": provider,
                    "modelId": model,
                    "options": {"reasoningLevel": self._reasoning},
                },
            },
        )
        accepted = bool((sent.get("result") or {}).get("accepted"))
        if not accepted:
            logger.warning("ZCode refused the message for %s: %s", session_id, sent.get("error"))
        return accepted

    @staticmethod
    async def _workspace(bridge: Bridge, session_id: str) -> dict | None:
        """The workspace a session belongs to, which `resume` will not do without."""
        listed = await bridge.call("session/list", {})
        for session in (listed.get("result") or {}).get("sessions") or []:
            if isinstance(session, dict) and session.get("sessionId") == session_id:
                return session.get("workspace")
        return None

    async def _hold(self, session_id: str, bridge: Bridge, when_done: LateFailure | None) -> None:
        """Keep the gate answering until the turn ends, then say how it ended."""
        try:
            if not await bridge.ended(TURN_SECONDS):
                logger.warning("The ZCode turn in %s is still going after hours", session_id)
            if bridge.failure:
                logger.warning("The ZCode turn in %s failed: %s", session_id, bridge.failure)
                if when_done is not None:
                    await when_done(bridge.failure)
            else:
                logger.info("The ZCode turn in %s ended", session_id)
        finally:
            self._running.pop(session_id, None)
            await bridge.close()


async def _refuse(asked: Permission) -> Answer:
    """What a runner with no gate behind it answers: nothing may run.

    Not a default anybody should reach — the runtime is built with an `Asking`
    — but a bridge that answers nothing hangs the turn, and a bridge that
    allows by default would be the gate failing open.
    """
    logger.warning("No gate is wired to ZCode; refusing %s", asked.tool)
    return Answer(decision="deny", reason="Halyard has no gate wired to this seat")
