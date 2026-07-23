"""Pending approvals: the thing a blocked agent is actually waiting on.

A hook bridge makes a blocking HTTP call and holds it open while a human decides.
This module owns that wait. Each request gets an identifier, a single-use nonce,
and a deadline; a channel resolves it, or the deadline does.

Everything here is built around one rule:

    **An approval that is not explicitly allowed is denied.**

Not "raises an error", not "stays pending" — denied, with a reason the agent can
read. `docs/hook-payload-notes.md` records why this has to be enforced here
rather than left to the caller: Claude Code treats a hook that crashes, times
out, or returns nothing as *no opinion* and runs the command anyway. Failing to
answer is indistinguishable from approving.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from halyard.core.events import RiskLevel, Role

Clock = Callable[[], datetime]

#: How long a decision stays reachable after it was made. A replayed button
#: press must be rejected, and it is rejected either way: while the record is
#: retained the store answers "already resolved", and once it is evicted the
#: store answers "unknown request". Both refuse. Retention only decides which
#: of the two explanations the user gets.
DEFAULT_RESOLVED_RETENTION = timedelta(hours=1)

#: Deliberately shorter than the bridge's HTTP timeout, which is itself shorter
#: than the hook timeout. A hook that outlives its timeout fails open, so the
#: store has to be the one that answers first. See `docs/hook-payload-notes.md`.
DEFAULT_TTL = timedelta(minutes=5)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class Decision(StrEnum):
    """The only two outcomes. There is no third, and no "pending" terminal state."""

    ALLOW = "allow"
    DENY = "deny"


class ResolutionReason(StrEnum):
    """Why an approval ended the way it did.

    Recorded in the audit log, and phrased back to the agent so a denial is
    actionable rather than mysterious.
    """

    #: A human pressed a button in time.
    USER = "user"
    #: Nobody answered before the deadline.
    TIMEOUT = "timeout"
    #: A decision arrived after the deadline had already passed.
    EXPIRED = "expired"
    #: The control plane stopped while the request was still open.
    SHUTDOWN = "shutdown"


class ApprovalRequest(BaseModel):
    """One tool call, waiting for a human.

    Immutable: the command a user is shown must be the command that runs. If
    this could be edited after the card was rendered, the thing approved and the
    thing executed would be two different objects.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    nonce: str
    session_id: str
    agent_id: str
    project: str
    tool: str
    command_summary: str
    command_full: str
    risk: RiskLevel
    expires_at: datetime
    created_at: datetime
    #: Claude Code's own per-tool-call identifier. Undocumented, but observed on
    #: every payload — see `docs/hook-payload-notes.md`. Used to recognise a
    #: retried request as the same tool call rather than a second one.
    tool_use_id: str | None = None
    role: Role | None = None
    #: The name the session carries in its app. Travels with the request
    #: because it is what identifies a *seat*, and a role no longer does: with
    #: a Claude driver and a Codex driver both configured, the role is the same
    #: for two seats whose cards belong in different places.
    session_name: str | None = None
    #: The agent's rationale. Nothing in a Phase 1 hook payload carries one:
    #: `tool_input.description` says what a command does, not why it is needed.
    #: Stays empty until Phase 2 can ask.
    reason: str | None = None


class ApprovalResolution(BaseModel):
    """How an approval ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    decision: Decision
    reason: ResolutionReason
    decided_at: datetime
    #: Channel-specific identity of whoever decided, when a human did.
    decided_by: str | None = None
    #: Human-readable explanation. Delivered to the agent verbatim, so it should
    #: read as something the agent can act on.
    note: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class ApprovalError(Exception):
    """Base class for every refusal this store can produce."""


class UnknownApprovalError(ApprovalError):
    """No such request — never created, or long since evicted."""


class AlreadyResolvedError(ApprovalError):
    """The request already has an outcome. Raised on a replayed button press."""


class InvalidNonceError(ApprovalError):
    """The supplied nonce does not match. Treat as hostile, not as a typo."""


class ApprovalExpiredError(ApprovalError):
    """A decision arrived after the deadline. The request is denied instead."""


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalResolution]
    resolution: ApprovalResolution | None = field(default=None)


class ApprovalStore:
    """Holds open approvals and the futures blocked on them.

    Async-safe: Claude Code dispatches independent tool calls in parallel, so
    several requests can be created, waited on, and resolved concurrently.
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
        tool: str,
        command_summary: str,
        command_full: str,
        risk: RiskLevel,
        tool_use_id: str | None = None,
        role: Role | None = None,
        session_name: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRequest:
        """Open a new approval, or return the one already open for this tool call.

        The identifier and nonce are generated here and never accepted from a
        caller, so there is exactly one place where the strength of the nonce is
        decided.

        A request carrying a `tool_use_id` that already has an open approval
        returns that approval untouched. A bridge whose HTTP call failed after
        the server had already handled it will retry, and without this the user
        would get two cards for one command — the second of which carries a live
        nonce that outlives the decision made on the first.
        """
        now = self._clock()
        async with self._lock:
            self._purge_expired_records(now)

            if tool_use_id is not None:
                existing = self._find_open_by_tool_use_id(tool_use_id, now)
                if existing is not None:
                    return existing.request

            request = ApprovalRequest(
                request_id=f"req_{uuid4().hex}",
                nonce=secrets.token_urlsafe(16),
                session_id=session_id,
                agent_id=agent_id,
                project=project,
                tool=tool,
                command_summary=command_summary,
                command_full=command_full,
                risk=risk,
                tool_use_id=tool_use_id,
                role=role,
                session_name=session_name,
                reason=reason,
                created_at=now,
                expires_at=now + self._ttl,
            )
            loop = asyncio.get_running_loop()
            self._pending[request.request_id] = _Pending(
                request=request, future=loop.create_future()
            )
            return request

    async def wait_for(self, request_id: str) -> ApprovalResolution:
        """Block until the request is decided, or until its deadline passes.

        Returns a denial on timeout rather than raising. A caller that has to
        remember to catch an exception in order to stay safe is a caller that
        will eventually forget.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownApprovalError(request_id)
            if pending.resolution is not None:
                return pending.resolution
            future = pending.future
            remaining = (pending.request.expires_at - self._clock()).total_seconds()

        try:
            # Shielded so that the timeout cancels this wait without destroying
            # the future itself, which is still the channel's way of delivering
            # a decision that lost the race by a hair.
            return await asyncio.wait_for(asyncio.shield(future), timeout=max(remaining, 0.0))
        except TimeoutError:
            return await self.deny(
                request_id,
                reason=ResolutionReason.TIMEOUT,
                note=(
                    "Denied: no response from the approver before the request expired. "
                    "Ask again if this is still needed."
                ),
            )

    async def resolve(
        self,
        request_id: str,
        *,
        nonce: str,
        decision: Decision,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> ApprovalResolution:
        """Record a human decision.

        Rejects, in order: an unknown request, one already decided, a bad nonce,
        and one whose deadline has passed. Nonce is checked before expiry so a
        caller without the nonce learns nothing about the request's state.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownApprovalError(request_id)
            if pending.resolution is not None:
                raise AlreadyResolvedError(request_id)

            # Constant-time: the nonce is the only thing standing between a
            # guessed request id and a granted approval.
            if not secrets.compare_digest(nonce, pending.request.nonce):
                raise InvalidNonceError(request_id)

            now = self._clock()
            if now >= pending.request.expires_at:
                # Close it out as denied so a late press cannot be followed by
                # a second, luckier one.
                self._settle(
                    pending,
                    Decision.DENY,
                    ResolutionReason.EXPIRED,
                    now,
                    decided_by=decided_by,
                    note="Denied: the approval had already expired when the decision arrived.",
                )
                raise ApprovalExpiredError(request_id)

            return self._settle(
                pending,
                decision,
                ResolutionReason.USER,
                now,
                decided_by=decided_by,
                note=note or self._default_note(decision, decided_by),
            )

    async def get(self, request_id: str) -> ApprovalRequest | None:
        """Return the request, or None if it is unknown or has been evicted."""
        async with self._lock:
            pending = self._pending.get(request_id)
            return pending.request if pending else None

    async def resolution_of(self, request_id: str) -> ApprovalResolution | None:
        """Return the outcome, or None if the request is still open or unknown."""
        async with self._lock:
            pending = self._pending.get(request_id)
            return pending.resolution if pending else None

    async def list_open(self) -> list[ApprovalRequest]:
        """Return every undecided request, oldest first."""
        async with self._lock:
            return sorted(
                (p.request for p in self._pending.values() if p.resolution is None),
                key=lambda r: r.created_at,
            )

    async def shutdown(self) -> None:
        """Deny everything still open.

        Called when the control plane stops. Any bridge still blocked on us gets
        an answer; without one it would wait until its own timeout and, past
        that, Claude Code would run the command unsupervised.
        """
        async with self._lock:
            now = self._clock()
            for pending in self._pending.values():
                if pending.resolution is None:
                    self._settle(
                        pending,
                        Decision.DENY,
                        ResolutionReason.SHUTDOWN,
                        now,
                        note="Denied: the Halyard control plane shut down while this was pending.",
                    )

    async def deny(
        self, request_id: str, *, reason: ResolutionReason, note: str
    ) -> ApprovalResolution:
        """Deny a request nobody decided, unless somebody just did.

        This is how the rest of the system fails closed once a request already
        exists — the deadline passing, the control plane stopping, or a step
        after creation failing in a way that means no human will ever see it.
        No nonce, because there is no human on this path.

        The deadline and a button press can land in the same instant. Whoever
        reaches the lock first wins, and a human who answered in time keeps
        their answer.
        """
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise UnknownApprovalError(request_id)
            if pending.resolution is not None:
                return pending.resolution
            return self._settle(pending, Decision.DENY, reason, self._clock(), note=note)

    def _settle(
        self,
        pending: _Pending,
        decision: Decision,
        reason: ResolutionReason,
        now: datetime,
        *,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> ApprovalResolution:
        """Write the outcome exactly once. Caller must hold the lock."""
        resolution = ApprovalResolution(
            request_id=pending.request.request_id,
            decision=decision,
            reason=reason,
            decided_at=now,
            decided_by=decided_by,
            note=note,
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
        """Drop decisions that are old enough to stop mattering.

        Only resolved records are evicted, and only well after the fact. The
        audit log is the durable history; this map exists to answer live
        callbacks. Caller must hold the lock.
        """
        cutoff = now - self._resolved_retention
        stale = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.resolution is not None and pending.resolution.decided_at < cutoff
        ]
        for request_id in stale:
            del self._pending[request_id]

    @staticmethod
    def _default_note(decision: Decision, decided_by: str | None) -> str:
        who = f" by {decided_by}" if decided_by else ""
        if decision is Decision.ALLOW:
            return f"Allowed{who} for this call only."
        return f"Denied{who}. Do not retry this command without asking first."
