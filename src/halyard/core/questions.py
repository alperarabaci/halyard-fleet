"""Pending questions: an agent asking a person to choose, not to approve.

The sibling of `approvals.py`, and its rule is inverted. An approval that goes
unanswered is *denied* — silence there is the dangerous direction, so it fails
closed. A question that goes unanswered simply falls back to where it already
was: the picker on the terminal, in front of whoever is at the desk. Silence
here is not dangerous, so it fails **open**.

That is the whole reason this is a separate store rather than a flag on the
other one. `Decision` has exactly two values and no third, on purpose, so that
the approval path can never record a resolution that was neither allow nor deny.
A question's answer is a *label the agent offered* — "Summary", "Detailed" — or
a sentence somebody typed instead, and neither is an allow or a deny. Folding
the two would put a value the approval store must never hold one refactor away
from holding it.

So the lifecycle is the same — an id, a single-use nonce, a deadline, a future
a blocked hook is waiting on — but the terminal states differ:

    answered   a person chose in time; the agent proceeds as if they had
    unanswered nobody chose; the hook says nothing and the desk picker appears
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from halyard.core.approvals import ResolutionReason
from halyard.core.events import Role

Clock = Callable[[], datetime]

#: The same bound the approval store uses, and for the same reason: shorter than
#: the bridge's HTTP timeout, which is shorter than the hook timeout, so the
#: store is always the layer that answers first. Past this the question is
#: unanswered and the terminal picker takes over.
DEFAULT_TTL = timedelta(minutes=5)

#: How long an answered question stays reachable, so a replayed button press
#: finds a resolved record ("already answered") rather than an empty one.
DEFAULT_RESOLVED_RETENTION = timedelta(hours=1)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class Choice(BaseModel):
    """One option the agent offered, as it was written on the card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    description: str | None = None


class QuestionRequest(BaseModel):
    """One `AskUserQuestion`, waiting for a person to choose.

    Immutable for the same reason an approval is: the options a person is shown
    must be the options the agent offered. Editing this after the card rendered
    would let the choice presented and the choice delivered drift apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    nonce: str
    session_id: str
    agent_id: str
    project: str
    #: The question text, verbatim. It is also the *key* the answer is filed
    #: under in `updatedInput.answers`, so it has to survive the round trip
    #: unchanged — measured against a live session, see the probe in the notes.
    question: str
    #: A short label for the question, when the tool gave one ("Format").
    header: str | None = None
    options: list[Choice]
    expires_at: datetime
    created_at: datetime
    #: Recognises a retried hook call as the same question rather than a second
    #: one, exactly as the approval store uses it.
    tool_use_id: str | None = None
    role: Role | None = None
    session_name: str | None = None


class QuestionResolution(BaseModel):
    """How a question ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    #: The chosen option's label, or a sentence typed instead of choosing, or
    #: None when nobody answered in time. None is the fail-open signal: the
    #: bridge says nothing and the terminal picker appears.
    answer: str | None
    reason: ResolutionReason
    decided_at: datetime
    decided_by: str | None = None

    @property
    def answered(self) -> bool:
        return self.answer is not None


class QuestionError(Exception):
    """Base class for every refusal this store can produce."""


class UnknownQuestionError(QuestionError):
    """No such question — never created, or long since evicted."""


class AlreadyAnsweredError(QuestionError):
    """The question already has an answer. Raised on a replayed button press."""


class InvalidNonceError(QuestionError):
    """The supplied nonce does not match. Treat as hostile, not as a typo."""


class QuestionExpiredError(QuestionError):
    """An answer arrived after the deadline. The question is left unanswered."""


@dataclass
class _Pending:
    request: QuestionRequest
    future: asyncio.Future[QuestionResolution]
    resolution: QuestionResolution | None = field(default=None)


class QuestionStore:
    """Holds open questions and the futures blocked on them.

    Async-safe, like the approval store beside it: several questions can be
    open, waited on and answered at once.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_TTL,
        resolved_retention: timedelta = DEFAULT_RESOLVED_RETENTION,
        clock: Clock = _default_clock,
    ) -> None:
        self._ttl = ttl
        self._resolved_retention = resolved_retention
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    async def create(
        self,
        *,
        session_id: str,
        agent_id: str,
        project: str,
        question: str,
        options: list[Choice],
        header: str | None = None,
        tool_use_id: str | None = None,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> QuestionRequest:
        """Open a new question, or return the one already open for this call.

        A retried hook call carrying a `tool_use_id` we already hold returns
        that question untouched, so a bridge that retried after the server had
        already handled it does not put two cards on the phone for one question.
        """
        now = self._clock()
        async with self._lock:
            self._purge_expired_records(now)

            if tool_use_id is not None:
                existing = self._find_open_by_tool_use_id(tool_use_id, now)
                if existing is not None:
                    return existing.request

            request = QuestionRequest(
                request_id=f"ask_{uuid4().hex}",
                nonce=secrets.token_urlsafe(16),
                session_id=session_id,
                agent_id=agent_id,
                project=project,
                question=question,
                header=header,
                options=list(options),
                tool_use_id=tool_use_id,
                role=role,
                session_name=session_name,
                created_at=now,
                expires_at=now + self._ttl,
            )
            loop = asyncio.get_running_loop()
            self._pending[request.request_id] = _Pending(
                request=request, future=loop.create_future()
            )
            return request

    async def wait_for(self, request_id: str) -> QuestionResolution:
        """Block until the question is answered, or its deadline passes.

        Returns an *unanswered* resolution on timeout rather than raising. The
        caller reads `answered` and, when it is false, tells the bridge to say
        nothing — which is how the terminal picker gets its turn.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownQuestionError(request_id)
            if pending.resolution is not None:
                return pending.resolution
            future = pending.future
            remaining = (pending.request.expires_at - self._clock()).total_seconds()

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=max(remaining, 0.0))
        except TimeoutError:
            return await self.give_up(
                request_id,
                reason=ResolutionReason.TIMEOUT,
            )

    async def answer(
        self,
        request_id: str,
        *,
        nonce: str,
        answer: str,
        decided_by: str | None = None,
    ) -> QuestionResolution:
        """Record a person's choice.

        Rejects, in order: an unknown question, one already answered, a bad
        nonce, and one whose deadline has passed. Nonce before expiry, so a
        caller without it learns nothing about the question's state.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownQuestionError(request_id)
            if pending.resolution is not None:
                raise AlreadyAnsweredError(request_id)

            if not secrets.compare_digest(nonce, pending.request.nonce):
                raise InvalidNonceError(request_id)

            now = self._clock()
            if now >= pending.request.expires_at:
                # Close it out as unanswered so a late press cannot be followed
                # by a second, luckier one — the same guard the approval store
                # keeps, with the fail-open terminal state instead of a denial.
                self._settle(pending, None, ResolutionReason.EXPIRED, now, decided_by=decided_by)
                raise QuestionExpiredError(request_id)

            return self._settle(pending, answer, ResolutionReason.USER, now, decided_by=decided_by)

    async def get(self, request_id: str) -> QuestionRequest | None:
        async with self._lock:
            pending = self._pending.get(request_id)
            return pending.request if pending else None

    async def list_open(self) -> list[QuestionRequest]:
        async with self._lock:
            return sorted(
                (p.request for p in self._pending.values() if p.resolution is None),
                key=lambda r: r.created_at,
            )

    async def shutdown(self) -> None:
        """Leave everything still open unanswered.

        Called when the control plane stops. A bridge blocked on us gets an
        answer of "nobody chose", which sends the question back to the terminal
        — the safe direction here, the way denying is the safe direction there.
        """
        async with self._lock:
            now = self._clock()
            for pending in self._pending.values():
                if pending.resolution is None:
                    self._settle(pending, None, ResolutionReason.SHUTDOWN, now)

    async def give_up(self, request_id: str, *, reason: ResolutionReason) -> QuestionResolution:
        """Resolve a question nobody answered as unanswered, unless one just did.

        The deadline and a button press can land in the same instant. Whoever
        reaches the lock first wins, and a person who chose in time keeps it.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownQuestionError(request_id)
            if pending.resolution is not None:
                return pending.resolution
            return self._settle(pending, None, reason, self._clock())

    def _settle(
        self,
        pending: _Pending,
        answer: str | None,
        reason: ResolutionReason,
        now: datetime,
        *,
        decided_by: str | None = None,
    ) -> QuestionResolution:
        """Write the outcome exactly once. Caller must hold the lock."""
        resolution = QuestionResolution(
            request_id=pending.request.request_id,
            answer=answer,
            reason=reason,
            decided_at=now,
            decided_by=decided_by,
        )
        pending.resolution = resolution
        if not pending.future.done():
            pending.future.set_result(resolution)
        return resolution

    def _find_open_by_tool_use_id(self, tool_use_id: str, now: datetime) -> _Pending | None:
        for pending in self._pending.values():
            if (
                pending.request.tool_use_id == tool_use_id
                and pending.resolution is None
                and now < pending.request.expires_at
            ):
                return pending
        return None

    def _purge_expired_records(self, now: datetime) -> None:
        cutoff = now - self._resolved_retention
        stale = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.resolution is not None and pending.resolution.decided_at < cutoff
        ]
        for request_id in stale:
            del self._pending[request_id]
