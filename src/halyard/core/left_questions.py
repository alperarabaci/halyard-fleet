"""Questions an agent asked and left waiting, carried to a phone and back.

The sibling of `QuestionService`, for a runtime that does not block on its
question. Codex Desktop asks with `request_user_input_async` and carries on, so
there is no hook waiting for an answer, and until now the question stayed on
the desk's screen where nobody away from it would see it.

The transcript watcher finds the question in the rollout; this puts a card up.
The card has no deadline, because the question on the desk has none. An answer
tapped or typed on the phone is handed to the runtime to deliver, and one given
at the desk closes the card, saying what was answered there.

Held in memory. A question asked before a restart is not carded after it: the
watcher starts at the end of a file it has not been reading.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from halyard.agents.spec import AskedQuestion
from halyard.core.approvals import ResolutionReason
from halyard.core.audit import AuditLog, question_answered, question_asked
from halyard.core.events import Role
from halyard.core.gate import Gate
from halyard.core.questions import Choice, QuestionRequest, QuestionStore, UnknownQuestionError
from halyard.core.registry import SessionRegistry

logger = logging.getLogger(__name__)

#: Hands an answer to the runtime that asked. Whether it took it.
Deliver = Callable[[QuestionRequest, str], Awaitable[bool]]


class LeftQuestions:
    """Cards for questions nobody is blocked on, and their answers."""

    def __init__(
        self,
        *,
        store: QuestionStore,
        audit: AuditLog,
        registry: SessionRegistry,
        channel,
        deliver: Deliver,
        project: str,
        gate: Gate | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._registry = registry
        self._channel = channel
        self._deliver = deliver
        self._project = project
        self._gate = gate or Gate()
        #: The runtime's own name for each question carded, to its card.
        self._carded: dict[str, str] = {}
        # Strong references, so a wait is not collected while it waits.
        self._waiting: set[asyncio.Task] = set()

    async def asked(
        self,
        *,
        session_id: str,
        agent_id: str,
        question: AskedQuestion,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> None:
        """Put a card up for a question just asked. Once per question."""
        if question.id in self._carded:
            return
        if self._gate.paused:
            # Paused means the phone is off; the question is still on the desk.
            logger.info("Paused, so not carding a question from %s", session_id)
            return
        seen = await self._registry.get(session_id)
        request = await self._store.create(
            session_id=session_id,
            agent_id=agent_id,
            project=seen.project if seen else self._project,
            question=question.title,
            options=[Choice(label=option) for option in question.options],
            tool_use_id=question.id,
            role=role,
            session_name=session_name,
            expires=False,
        )
        self._carded[question.id] = request.request_id
        await self._try_to_record(question_asked(request))
        try:
            await self._channel.send_question(request)
        except Exception:
            logger.exception("Could not put a question from %s on the phone", session_id)
            await self._store.give_up(request.request_id, reason=ResolutionReason.SHUTDOWN)
            return
        waiting = asyncio.create_task(self._carry(request))
        self._waiting.add(waiting)
        waiting.add_done_callback(self._waiting.discard)

    async def answered(self, question: str, answer: str) -> None:
        """A question was answered in the agent's own chat. Close its card.

        Also how an answer sent from the phone comes back, written into the
        transcript: that card is settled already, and stays as it is.
        """
        request_id = self._carded.pop(question, None)
        if request_id is not None:
            await self._close(request_id, answer)

    async def set_aside(self, session_id: str) -> None:
        """The person wrote something else in that chat, at the desk or from
        here, so every question it asked before is over unanswered."""
        for question, request_id in list(self._carded.items()):
            request = await self._store.get(request_id)
            if request is None or request.session_id != session_id:
                continue
            del self._carded[question]
            await self._close(request_id, None)

    async def _close(self, request_id: str, answer: str | None) -> None:
        """Settle a card from the agent's side, unless the phone already did."""
        request = await self._store.get(request_id)
        if request is None:
            return
        try:
            resolution = await self._store.give_up(request_id, reason=ResolutionReason.ELSEWHERE)
        except UnknownQuestionError:
            return
        if resolution.reason is not ResolutionReason.ELSEWHERE:
            return
        closing = getattr(self._channel, "close_question", None)
        if closing is not None:
            try:
                await closing(request, answer=answer, by="at the desk")
            except Exception:
                logger.warning("Could not close the card for %s", request_id, exc_info=True)

    async def _carry(self, request: QuestionRequest) -> None:
        """Wait for the phone's answer, and hand it to the runtime."""
        resolution = await self._store.wait_for(request.request_id)
        await self._try_to_record(question_answered(request, resolution))
        if resolution.reason is not ResolutionReason.USER or resolution.answer is None:
            return
        try:
            delivered = await self._deliver(request, resolution.answer)
        except Exception:
            logger.exception("Could not deliver an answer to %s", request.session_id)
            delivered = False
        if delivered:
            return
        try:
            await self._channel.send_message(
                request.session_id,
                "⚠️ That answer did not reach the agent. It is still waiting at the desk.",
                request.role,
                agent_id=request.agent_id,
                session_name=request.session_name,
                project=request.project,
            )
        except Exception:
            logger.debug("Could not say an answer was not delivered", exc_info=True)

    async def _try_to_record(self, record) -> None:
        # Best effort, as for any question: it only asks somebody something.
        try:
            await self._audit.record(record)
        except Exception:
            logger.warning("Audit write failed for %s", record.action.value, exc_info=True)
