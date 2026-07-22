"""The path a permission request takes, start to finish.

Redact, classify, record, ask, wait, record again. This lives in core rather
than in a web handler because it is the part that has to be right: every step
that can fail has a defined answer, and that answer is always the same one.

    Anything that goes wrong produces a denial, not an exception.

`request()` does not raise. It cannot, safely — the caller is an HTTP handler
speaking to a hook bridge, and `docs/hook-payload-notes.md` records what Claude
Code does with a hook that fails to answer cleanly: it runs the command. An
exception escaping this method would eventually become an approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from halyard.core.approvals import (
    ApprovalRequest,
    ApprovalStore,
    ResolutionReason,
)
from halyard.core.audit import (
    AuditLog,
    agent_message,
    approval_requested,
    approval_resolved,
    bridge_error,
)
from halyard.core.events import RiskLevel, Role
from halyard.core.gate import Gate
from halyard.core.policy import Policy
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry

logger = logging.getLogger(__name__)


def project_name(project_dir: str | None, cwd: str | None, configured: str) -> str:
    """What to call the project a request came from.

    `CLAUDE_PROJECT_NAME` is one value in one control plane, so on its own it
    labels every card with the same name however many repositories are wired to
    it. Gate a second project and its approvals arrive wearing the first one's
    name — which was found in real use, with a command from `agent-platform`
    arriving on a phone as `alpha-engine`. An approver who cannot tell which
    codebase a command belongs to cannot meaningfully approve it, and that is
    the whole premise.

    Read from the path rather than the filesystem: the control plane usually
    runs in a container and cannot see the host's directories, so this has only
    the string to work with.
    """
    for path in (project_dir, cwd):
        if not path:
            continue
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name:
            return name
    return configured


def seat_of(
    role: Role | None,
    session_name: str | None,
    seats: dict[str, Role] | None,
) -> Role | None:
    """Which seat a request came from.

    Two ways in, because two kinds of session exist. One launched from a shell
    can say so directly — `HALYARD_ROLE=navigator claude` — and that wins,
    because it is the more explicit statement. One started from the desktop app
    has no shell to say it in, so it is recognised by its name instead.

    The name is the workable key rather than `session_id`, which is a fresh
    UUID on every restart. A named conversation keeps its name, so this is
    configured once instead of re-paired every morning.

    Matched case-insensitively and trimmed, because it is copied by hand.
    """
    if role is not None:
        return role
    if not session_name or not seats:
        return None
    return seats.get(session_name.strip().casefold())


class BridgeDecision(StrEnum):
    """What the bridge is told to do.

    Three values here, where the approval store has two. An approval is only
    ever allowed or denied — but the bridge can also be told that no approval
    happened at all, which is what a paused gate means. Keeping that third value
    out of `Decision` keeps the store's invariant honest: it never records a
    resolution that was neither.
    """

    ALLOW = "allow"
    DENY = "deny"
    #: Halyard is not answering. Claude Code falls back to its own permission
    #: prompt — the question moves back to the terminal rather than vanishing.
    DEFER = "defer"


@dataclass(frozen=True)
class ApprovalOutcome:
    """What the bridge gets back."""

    decision: BridgeDecision
    #: Handed to Claude Code as the denial reason and shown to the user, so it
    #: is written for an agent to act on rather than for a log to be grepped.
    reason: str
    request_id: str | None = None
    risk: RiskLevel | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is BridgeDecision.ALLOW


class MessageRelay:
    """Carries an agent's own words out to a channel.

    The mirror image of `ApprovalService`, and the rule is inverted. An approval
    that cannot be delivered must deny, because a command is waiting on it.
    A message that cannot be delivered is a lost notification, and stalling the
    agent's turn over one would cost more than the message is worth. So this
    reports failure instead of enforcing anything, and never raises.

    Redaction still applies. The text is about to leave the machine for somebody
    else's servers, which is the same reason approval cards are masked — an
    agent quoting a command it just ran can quote a credential along with it.
    """

    def __init__(
        self,
        *,
        redactor: Redactor,
        registry: SessionRegistry,
        audit: AuditLog,
        channel,
        project: str,
        gate: Gate | None = None,
        seats: dict[str, Role] | None = None,
    ) -> None:
        self._seats = seats or {}
        self._redactor = redactor
        self._registry = registry
        self._audit = audit
        self._channel = channel
        self._project = project
        self._gate = gate or Gate()

    async def relay(
        self,
        *,
        session_id: str,
        agent_id: str,
        text: str,
        cwd: str | None = None,
        project_dir: str | None = None,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> bool:
        """Send an agent's reply out. Returns whether it was delivered."""
        if self._gate.paused:
            # Pausing means the phone is off, not that approvals alone stop.
            # Someone who has taken the decisions back to the keyboard does not
            # want the replies buzzing on a device they are not looking at.
            return False

        project = project_name(project_dir, cwd, self._project)
        role = seat_of(role, session_name, self._seats)
        try:
            masked = self._redactor.redact(text)
            await self._registry.observe(
                session_id=session_id,
                agent_id=agent_id,
                project=project,
                role=role,
                cwd=cwd,
            )
            delivered = await self._deliver(session_id, masked.text, role)
        except Exception:
            logger.exception("Could not relay a message from %s", session_id)
            return False

        try:
            await self._audit.record(
                agent_message(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    length=len(masked.text),
                    redacted=masked.redacted,
                    delivered=delivered,
                )
            )
        except Exception:
            # An unrecorded message is not an unrecorded decision. It does not
            # change what happened, and there is nothing to undo.
            logger.warning("Could not record a relayed message", exc_info=True)
        return delivered

    async def _deliver(self, session_id: str, text: str, role: Role | None) -> bool:
        try:
            # Always as messages, however long. A reply arriving as a file has
            # to be tapped, downloaded and opened, and reading it where it
            # lands is the entire point. The channel splits if it must.
            await self._channel.send_message(session_id, text, role)
        except Exception:
            logger.exception("Channel refused a relayed message from %s", session_id)
            return False
        return True


class ApprovalService:
    """Runs one permission request from arrival to answer."""

    def __init__(
        self,
        *,
        store: ApprovalStore,
        policy: Policy,
        redactor: Redactor,
        audit: AuditLog,
        registry: SessionRegistry,
        channel,
        project: str,
        gate: Gate | None = None,
        seats: dict[str, Role] | None = None,
    ) -> None:
        self._seats = seats or {}
        self._store = store
        self._gate = gate or Gate()
        self._policy = policy
        self._redactor = redactor
        self._audit = audit
        self._registry = registry
        self._channel = channel
        self._project = project

    async def request(
        self,
        *,
        session_id: str,
        agent_id: str,
        tool: str,
        command: str,
        tool_use_id: str | None = None,
        cwd: str | None = None,
        project_dir: str | None = None,
        role: Role | None = None,
        session_name: str | None = None,
        reason: str | None = None,
        declared_risk: RiskLevel | None = None,
    ) -> ApprovalOutcome:
        """Ask for permission, and answer. Never raises."""
        try:
            return await self._request(
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                command=command,
                tool_use_id=tool_use_id,
                cwd=cwd,
                project_dir=project_dir,
                role=role,
                session_name=session_name,
                reason=reason,
                declared_risk=declared_risk,
            )
        except Exception:
            # The outer net. Anything not handled below still has to come out of
            # here as a denial, because the alternative is a 500 and a hook that
            # shrugs and runs the command.
            logger.exception("Approval request failed unexpectedly; denying")
            await self._try_to_record(
                bridge_error(message="unhandled error while processing", session_id=session_id)
            )
            return ApprovalOutcome(
                decision=BridgeDecision.DENY,
                reason=(
                    "Denied: the Halyard control plane hit an internal error and failed "
                    "closed. Nothing was approved."
                ),
            )

    async def _request(
        self,
        *,
        session_id: str,
        agent_id: str,
        tool: str,
        command: str,
        tool_use_id: str | None,
        cwd: str | None,
        project_dir: str | None,
        role: Role | None,
        session_name: str | None,
        reason: str | None,
        declared_risk: RiskLevel | None,
    ) -> ApprovalOutcome:
        # Redaction first, before the command is copied anywhere. Everything
        # downstream — policy, the store, the audit log, the card — sees only
        # what comes out of here.
        if self._gate.paused:
            # Nothing is created, nothing is asked, nothing is decided. Claude
            # Code falls back to its own permission prompt, which is where the
            # question lived before Halyard existed. Deferring is not approving,
            # and this path must never become one.
            return ApprovalOutcome(
                decision=BridgeDecision.DEFER,
                reason="Halyard is paused; this was not relayed for approval.",
            )

        prepared = self._redactor.prepare(command)
        classification = self._policy.classify(prepared.full, declared=declared_risk)
        project = project_name(project_dir, cwd, self._project)
        role = seat_of(role, session_name, self._seats)

        await self._registry.observe(
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            role=role,
            cwd=cwd,
        )

        request = await self._store.create(
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            tool=tool,
            command_summary=prepared.summary,
            command_full=prepared.full,
            risk=classification.risk,
            tool_use_id=tool_use_id,
            role=role,
            reason=reason,
        )

        # Record that it was asked before anybody can act on it. An approval
        # that was never written down is one nobody can account for afterwards.
        if not await self._try_to_record(approval_requested(request)):
            return await self._fail_closed(
                request,
                ResolutionReason.SHUTDOWN,
                "Denied: the Halyard audit log could not be written, so nothing was approved.",
            )

        try:
            await self._channel.send_approval_request(request)
        except Exception:
            logger.exception("Channel refused the approval request; denying")
            await self._try_to_record(
                bridge_error(
                    message="approval could not be delivered to the channel",
                    session_id=session_id,
                    request_id=request.request_id,
                )
            )
            return await self._fail_closed(
                request,
                ResolutionReason.SHUTDOWN,
                "Denied: the approval could not be delivered to anyone, so nobody saw it.",
            )

        # Blocks until a human answers or the deadline passes. Returns a
        # denial on timeout; it does not raise.
        resolution = await self._store.wait_for(request.request_id)

        recorded = await self._try_to_record(approval_resolved(request, resolution))
        if not recorded and resolution.allowed:
            # A denial that went unrecorded is still a denial, so it stands. An
            # approval that went unrecorded is a command about to run with no
            # trace of who agreed to it, which is not something to let through.
            return ApprovalOutcome(
                decision=BridgeDecision.DENY,
                reason=(
                    "Denied: the approval was granted but could not be written to the "
                    "audit log, so it was not honoured."
                ),
                request_id=request.request_id,
                risk=request.risk,
            )

        return ApprovalOutcome(
            decision=BridgeDecision(resolution.decision.value),
            reason=resolution.note or f"{resolution.decision.value} ({resolution.reason.value})",
            request_id=request.request_id,
            risk=request.risk,
        )

    async def _fail_closed(
        self, request: ApprovalRequest, reason: ResolutionReason, message: str
    ) -> ApprovalOutcome:
        """Close out a request that will never reach a human."""
        try:
            await self._store.deny(request.request_id, reason=reason, note=message)
        except Exception:
            # Already resolved, or already gone. Either way the answer below is
            # the safe one, so there is nothing to do about it.
            logger.debug("Could not close out %s", request.request_id, exc_info=True)
        return ApprovalOutcome(
            decision=BridgeDecision.DENY,
            reason=message,
            request_id=request.request_id,
            risk=request.risk,
        )

    async def _try_to_record(self, record) -> bool:
        """Write to the audit log, reporting failure instead of raising.

        Callers decide what a failed write means; for most of them it means
        denying. This method only refuses to make that decision for them.
        """
        try:
            await self._audit.record(record)
        except Exception:
            logger.exception("Audit write failed for %s", record.action.value)
            return False
        return True
