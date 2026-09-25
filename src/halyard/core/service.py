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

import asyncio
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from halyard.core import grants, reads, refusals
from halyard.core.approvals import (
    ApprovalRequest,
    ApprovalStore,
    Decision,
    ResolutionReason,
)
from halyard.core.audit import (
    AuditLog,
    agent_message,
    approval_requested,
    approval_resolved,
    bridge_error,
    question_answered,
    question_asked,
    refused_outright,
    risk_preauthorized,
    tool_preauthorized,
    write_preauthorized,
)
from halyard.core.events import RiskLevel, Role
from halyard.core.gate import Gate
from halyard.core.policy import Policy
from halyard.core.questions import Choice, QuestionStore
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


@dataclass(frozen=True)
class QuestionOutcome:
    """What the bridge gets back for an `AskUserQuestion`.

    One field, and its absence is the load-bearing case. An answer means the
    bridge fills it into `updatedInput.answers` and the agent proceeds as if the
    person had chosen at the desk. No answer means the bridge says nothing, and
    the terminal picker — which never went away — takes the choice instead.
    """

    #: The chosen label, or a sentence typed instead of choosing. None when
    #: nobody answered in time, the gate is paused, or delivery failed: every
    #: one of those sends the question back to the terminal rather than deciding
    #: it, because a question is not dangerous to leave unanswered the way a
    #: command is dangerous to leave unapproved.
    answer: str | None


class QuestionService:
    """Runs one `AskUserQuestion` from arrival to answer.

    The sibling of `ApprovalService`, failing **open** where that fails closed.
    Every path that cannot produce a chosen answer returns `answer=None`, which
    tells the bridge to stay quiet so the desk picker appears. It never raises,
    for the same reason the approval service never raises — the caller is an
    HTTP handler in front of a blocked hook.

    Nothing here redacts. The approval service masks the command before it
    leaves the machine; here the question text and the option labels have to
    travel *verbatim*, because the text is the key and the label is the value
    that `updatedInput.answers` is matched on — measured against a live session.
    Masking either would break the round trip, and a question is the agent's own
    words asking a person to choose, not the output of a command it just ran.
    """

    def __init__(
        self,
        *,
        store: QuestionStore,
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
        self._audit = audit
        self._registry = registry
        self._channel = channel
        self._project = project

    async def ask(
        self,
        *,
        session_id: str,
        agent_id: str,
        question: str,
        options: list[Choice],
        header: str | None = None,
        tool_use_id: str | None = None,
        cwd: str | None = None,
        project_dir: str | None = None,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> QuestionOutcome:
        """Ask a person to choose, and answer. Never raises."""
        try:
            return await self._ask(
                session_id=session_id,
                agent_id=agent_id,
                question=question,
                options=options,
                header=header,
                tool_use_id=tool_use_id,
                cwd=cwd,
                project_dir=project_dir,
                role=role,
                session_name=session_name,
            )
        except Exception:
            # The outer net. Anything unhandled becomes "no answer", which is the
            # safe direction here: the terminal picker is still standing and gets
            # the choice.
            logger.exception("Question failed unexpectedly; falling back to the terminal")
            return QuestionOutcome(answer=None)

    async def _ask(
        self,
        *,
        session_id: str,
        agent_id: str,
        question: str,
        options: list[Choice],
        header: str | None,
        tool_use_id: str | None,
        cwd: str | None,
        project_dir: str | None,
        role: Role | None,
        session_name: str | None,
    ) -> QuestionOutcome:
        project = project_name(project_dir, cwd, self._project)
        role = seat_of(role, session_name, self._seats)

        # Seen before the pause, like every call; see `ApprovalService`.
        await self._registry.observe(
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            role=role,
            session_name=session_name,
            cwd=cwd,
        )

        if self._gate.paused:
            # Pausing means the phone is off. The choice belongs at the desk,
            # which is exactly where the terminal picker will put it.
            return QuestionOutcome(answer=None)

        request = await self._store.create(
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            question=question,
            options=options,
            header=header,
            tool_use_id=tool_use_id,
            role=role,
            session_name=session_name,
        )

        # Best effort, unlike the approval path. An approval that cannot be
        # recorded is denied, because a command must not run unaccounted for; a
        # question that cannot be recorded still only *asks* somebody to pick,
        # and refusing to ask would send them to the desk over a logging blip.
        await self._try_to_record(question_asked(request))

        try:
            await self._channel.send_question(request)
        except Exception:
            logger.exception("Channel refused the question; falling back to the terminal")
            await self._store.give_up(request.request_id, reason=ResolutionReason.SHUTDOWN)
            return QuestionOutcome(answer=None)

        # Blocks until a person chooses or the deadline passes. Returns an
        # unanswered resolution on timeout; it does not raise.
        resolution = await self._store.wait_for(request.request_id)
        await self._try_to_record(question_answered(request, resolution))
        return QuestionOutcome(answer=resolution.answer)

    async def _try_to_record(self, record) -> bool:
        try:
            await self._audit.record(record)
        except Exception:
            logger.exception("Audit write failed for %s", record.action.value)
            return False
        return True


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
        project = project_name(project_dir, cwd, self._project)
        role = seat_of(role, session_name, self._seats)
        if (own := self._registry.own(session_id)) is not None:
            # A turn Halyard started for itself: whoever started it has the
            # answer already. Relayed, it arrived in the chat as a stranger's
            # reply, and — seen as an agent at work — put that runtime's label
            # on the task the branch was for.
            logger.info(
                "Reply from Halyard's own turn %s (%s) kept out of the chat", session_id, own
            )
            await self._try_to_record_message(session_id, agent_id, project, len(text))
            return False
        try:
            # Seen before the pause, like every call; see `ApprovalService`.
            await self._registry.observe(
                session_id=session_id,
                agent_id=agent_id,
                project=project,
                role=role,
                session_name=session_name,
                cwd=cwd,
            )
            if self._gate.paused:
                # Pausing means the phone is off, not that approvals alone stop.
                # Someone who has taken the decisions back to the keyboard does
                # not want the replies buzzing on a device they are not looking at.
                return False
            masked = self._redactor.redact(text)
            delivered = await self._deliver(
                session_id,
                masked.text,
                role,
                agent_id=agent_id,
                session_name=session_name,
                project=project,
            )
        except Exception:
            logger.exception("Could not relay a message from %s", session_id)
            return False

        await self._try_to_record_message(
            session_id, agent_id, project, len(masked.text), redacted=masked.redacted,
            delivered=delivered,
        )  # fmt: skip
        return delivered

    async def _try_to_record_message(
        self,
        session_id: str,
        agent_id: str,
        project: str,
        length: int,
        *,
        redacted: bool = False,
        delivered: bool = False,
    ) -> None:
        try:
            await self._audit.record(
                agent_message(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    length=length,
                    redacted=redacted,
                    delivered=delivered,
                )
            )
        except Exception:
            # An unrecorded message is not an unrecorded decision. It does not
            # change what happened, and there is nothing to undo.
            logger.warning("Could not record a relayed message", exc_info=True)

    async def _deliver(
        self,
        session_id: str,
        text: str,
        role: Role | None,
        *,
        agent_id: str,
        session_name: str | None,
        project: str | None = None,
    ) -> bool:
        try:
            # Always as messages, however long. A reply arriving as a file has
            # to be tapped, downloaded and opened, and reading it where it
            # lands is the entire point. The channel splits if it must.
            await self._channel.send_message(
                session_id,
                text,
                role,
                agent_id=agent_id,
                session_name=session_name,
                project=project,
            )
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
        allowed_writes: tuple[str, ...] = (),
        allowed_tools: tuple[str, ...] = (),
        refuse_agent_commits: bool = False,
        allow_risk_at_or_below: RiskLevel | None = None,
        runs_by_project: Mapping[str, Sequence[str]] | None = None,
        trusted_runs: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._seats = seats or {}
        # Each configured project's `runs:`, by where the project is — every
        # project, since the table may hold entries for one the file has none
        # for. An entry that no longer reads is dropped here too.
        self._parsed: dict[str, reads.Run | None] = {}
        self._runs: dict[str, tuple[reads.Run, ...]] = {
            os.path.realpath(os.path.expanduser(path)): self._entries(texts)
            for path, texts in (runs_by_project or {}).items()
        }
        #: The same project's entries kept in the database, read each time:
        #: one added from the command line applies without a restart. Given the
        #: project's path as a key of `runs_by_project`.
        self._trusted = trusted_runs
        self._store = store
        self._gate = gate or Gate()
        self._policy = policy
        #: Off unless somebody says otherwise, so nothing changes for anybody
        #: who has not asked for it. See `core/refusals.py`.
        self._refuse_agent_commits = refuse_agent_commits
        #: What goes through without a card: paths under `writes:`, tools
        #: under `tools:`, and — with the setting on — shell commands `reads`
        #: understands. All empty or off by default, which is how this shipped.
        self._rules = grants.Rules(
            tools=tuple(allowed_tools),
            writes=tuple(allowed_writes),
            reads=allow_risk_at_or_below is not None,
        )
        self._redactor = redactor
        self._audit = audit
        self._registry = registry
        self._channel = channel
        self._project = project

    async def _recorded(
        self,
        grant: grants.Grant,
        *,
        session_id: str,
        agent_id: str,
        project: str,
        tool: str,
        matched: tuple[str, ...],
        command: str,
        cwd: str,
    ) -> bool:
        """Write a grant down, every part of it. False when any of it could not
        be, which makes it a card."""
        if grant.kind == "tools":
            return await self._try_to_record(
                tool_preauthorized(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    tool=tool,
                    pattern=grant.pattern,
                )
            )
        if grant.kind == "reads":
            return await self._try_to_record(
                risk_preauthorized(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    tool=tool,
                    matched=matched,
                    command=command,
                    cwd=cwd,
                    why=grant.why,
                    rules=reads.VERSION,
                )
            )
        recorded = [
            await self._try_to_record(
                write_preauthorized(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    tool=tool,
                    file_path=path,
                    pattern=pattern,
                )
            )
            for path, pattern in zip(grant.paths, grant.patterns, strict=True)
        ]
        return all(recorded)

    def _entries(self, texts: Sequence[str]) -> tuple[reads.Run, ...]:
        """Entries as `reads` takes them, each parsed once however often it is
        read back. One that no longer stands is left out, and said once."""
        kept = []
        for text in texts:
            if text not in self._parsed:
                try:
                    self._parsed[text] = reads.run_entry(text)
                except ValueError as why:
                    logger.warning("Ignoring a `runs:` entry: %s", why)
                    self._parsed[text] = None
            if (parsed := self._parsed[text]) is not None:
                kept.append(parsed)
        return tuple(kept)

    async def _runs_for(self, root: str | None) -> tuple[reads.Run, ...]:
        """What the configured project this directory is in trusts to run — the
        innermost, when one project sits inside another: its `runs:`, and the
        entries kept for it in the database."""
        if not root or not self._runs:
            return ()
        here = os.path.realpath(os.path.expanduser(root))
        inside = [path for path in self._runs if os.path.commonpath([here, path]) == path]
        if not inside:
            return ()
        found = max(inside, key=len)
        kept = await asyncio.to_thread(self._trusted, found) if self._trusted else ()
        return (*self._runs[found], *self._entries(kept))

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
        file_path: str | None = None,
        asks: str | None = None,
        patterns: list[str] | None = None,
        file_paths: list[str] | None = None,
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
                file_path=file_path,
                asks=asks,
                patterns=patterns,
                file_paths=file_paths,
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

    async def answered_elsewhere(
        self, *, session_id: str, agent_id: str, tool_use_id: str, decision: Decision
    ) -> bool:
        """A card's question was answered where the agent runs. Close the card.

        This decides nothing: the runtime has already acted on what was said at
        the desk. It records that, and takes the buttons off a card that would
        otherwise go on asking a settled question until it expired. The answer
        is whether a card was still open — it is not, when the phone got there
        first.
        """
        where = f"{agent_id}, at the desk"
        request = await self._store.answered_elsewhere(
            session_id=session_id,
            tool_use_id=tool_use_id,
            decision=decision,
            decided_by=where,
            note=(
                f"{'Allowed' if decision is Decision.ALLOW else 'Denied'} in {agent_id} "
                "itself, before anybody answered here."
            ),
        )
        if request is None:
            return False
        closing = getattr(self._channel, "close_approval", None)
        if closing is not None:
            try:
                await closing(request, decision=decision.value, by=where)
            except Exception:
                logger.warning("Could not close the card for %s", request.request_id, exc_info=True)
        return True

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
        file_path: str | None = None,
        asks: str | None = None,
        patterns: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> ApprovalOutcome:
        project = project_name(project_dir, cwd, self._project)
        role = seat_of(role, session_name, self._seats)

        # Seen first, whatever becomes of the call. Refused, deferred by a pause
        # or allowed without asking, it is still a seat at work, and what listens
        # keeps a record of who worked where. Seen only on the way to a card, that
        # record had gaps exactly where nobody was asked: a reviewer running
        # read-only commands could spend an afternoon on a task and never be seen.
        #
        # Except a turn Halyard started for itself, which is no seat at all: seen,
        # it put its runtime's label on the task. Everything after this — the
        # rules, the card — is the same for it as for anyone.
        if self._registry.own(session_id) is None:
            await self._registry.observe(
                session_id=session_id,
                agent_id=agent_id,
                project=project,
                role=role,
                session_name=session_name,
                cwd=cwd,
            )

        # Before anything is decided, including the pause. This is not an
        # approval somebody could be asked for and it is not a grant that could
        # be configured around — it is a standing answer, and a guard a pause
        # quietly switches off is a guard nobody can rely on.
        if act := refusals.writes_history_if(command, self._refuse_agent_commits):
            await self._try_to_record(
                refused_outright(
                    session_id=session_id,
                    agent_id=agent_id,
                    project=project,
                    tool=tool,
                    act=act,
                )
            )
            return ApprovalOutcome(decision=BridgeDecision.DENY, reason=refusals.why(act))

        if self._gate.paused:
            # Nothing is created, nothing is asked, nothing is decided. Claude
            # Code falls back to its own permission prompt, which is where the
            # question lived before Halyard existed. Deferring is not approving,
            # and this path must never become one.
            return ApprovalOutcome(
                decision=BridgeDecision.DEFER,
                reason="Halyard is paused; this was not relayed for approval.",
            )

        # What is shown and kept is redacted; what is judged is the command as
        # it will run. Judged redacted, `TOKEN=$(python3 x.py) git status`
        # arrived as `TOKEN=*** git status` and read as a harmless status.
        prepared = self._redactor.prepare(command)
        classification = self._policy.classify(command, declared=declared_risk)

        # Whether it goes through without a card is decided in `grants.py`, in
        # one place, so what `halyard upkeep` works out from the log is what
        # happens here. Only a shell command needs the project's trusted
        # commands, and only then are they read.
        runs = (
            await self._runs_for(project_dir or cwd)
            if grants.shell_judged(tool, self._rules)
            else ()
        )
        grant = grants.grant_for(
            tool=tool,
            command=command,
            risk=classification.risk,
            cwd=cwd,
            project_dir=project_dir,
            rules=self._rules,
            runs=runs,
            file_path=file_path,
            file_paths=file_paths,
        )
        # A grant goes through only once it is written down: one the audit log
        # could not take is a card instead.
        if grant is not None and await self._recorded(
            grant,
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            tool=tool,
            matched=classification.matched,
            command=prepared.full,
            cwd=cwd or project_dir or "",
        ):
            return ApprovalOutcome(
                decision=BridgeDecision.ALLOW, reason=grant.reason, risk=classification.risk
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
            session_name=session_name,
            reason=reason,
            asks=asks,
            patterns=patterns,
            cwd=cwd,
            project_dir=project_dir,
            redacted=prepared.full != command,
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
        if resolution.reason is ResolutionReason.ELSEWHERE:
            # Answered at the desk, in the runtime's own prompt, and acted on
            # there already. Handed back as an allow, anything able to post
            # "answered at the desk" could approve a command; so the bridge is
            # told what a pause tells it — this was not Halyard's to answer.
            return ApprovalOutcome(
                decision=BridgeDecision.DEFER,
                reason=resolution.note or "Answered where the agent runs.",
                request_id=request.request_id,
                risk=request.risk,
            )
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
