"""Tests for the end-to-end approval path.

Mostly about what happens when something breaks. The happy path is one test;
the rest are the ways this could quietly start approving things.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from halyard.channels.stub import StubChannel
from halyard.core.approvals import ApprovalRequest, ApprovalStore, Decision
from halyard.core.audit import AuditAction, AuditLog, AuditRecord, JsonlAuditSink
from halyard.core.events import RiskLevel
from halyard.core.policy import Policy
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry
from halyard.core.service import ApprovalService

SECRET = "hunter2SuperSecretValue"


class BrokenSink:
    """An audit sink that never accepts anything."""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, record: AuditRecord) -> None:
        raise OSError("disk full")


class ExplodingChannel:
    """A channel that cannot deliver."""

    name = "exploding"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_approval_request(self, request: ApprovalRequest) -> str:
        raise ConnectionError("telegram unreachable")

    async def send_message(
        self, session_id: str, text: str, role=None, *, agent_id=None, session_name=None
    ) -> str: ...
    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str: ...


class SilentChannel:
    """A channel that accepts the request and then never answers."""

    name = "silent"
    last_request: ApprovalRequest | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_approval_request(self, request: ApprovalRequest) -> str:
        self.last_request = request
        return "sent"

    async def send_message(
        self, session_id: str, text: str, role=None, *, agent_id=None, session_name=None
    ) -> str: ...
    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str: ...


def build_service(
    tmp_path: Path,
    *,
    channel=None,
    store: ApprovalStore | None = None,
    audit: AuditLog | None = None,
    ttl: timedelta = timedelta(minutes=5),
) -> tuple[ApprovalService, ApprovalStore, JsonlAuditSink]:
    store = store or ApprovalStore(ttl=ttl)
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = audit or AuditLog([sink])
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=audit,
        registry=SessionRegistry(),
        channel=channel if channel is not None else StubChannel(store, Decision.ALLOW),
        project="alpha-engine",
    )
    return service, store, sink


async def ask(service: ApprovalService, command: str = "git status", **kwargs):
    defaults = {"session_id": "session-1", "agent_id": "claude-code", "tool": "Bash"}
    return await service.request(command=command, **{**defaults, **kwargs})


# --- the path works ---------------------------------------------------------


async def test_an_approved_request_comes_back_allowed(tmp_path: Path) -> None:
    service, _, sink = build_service(tmp_path)
    await sink.open()

    outcome = await ask(service, "git status")

    assert outcome.allowed
    assert outcome.request_id is not None
    assert outcome.risk is RiskLevel.LOW
    assert [r.action for r in await sink.read_all()] == [
        AuditAction.APPROVAL_REQUESTED,
        AuditAction.APPROVAL_RESOLVED,
    ]


async def test_a_refused_request_comes_back_denied(tmp_path: Path) -> None:
    store = ApprovalStore()
    service, _, sink = build_service(
        tmp_path, store=store, channel=StubChannel(store, Decision.DENY)
    )
    await sink.open()

    outcome = await ask(service, "rm -rf build")

    assert not outcome.allowed
    assert outcome.risk is RiskLevel.HIGH


async def test_the_command_is_classified_before_anyone_sees_it(tmp_path: Path) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "docker compose down postgres")

    assert channel.last_request is not None
    assert channel.last_request.risk is RiskLevel.HIGH


async def test_secrets_are_masked_before_the_channel_or_the_log_see_them(
    tmp_path: Path,
) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, f"psql postgres://alper:{SECRET}@db/alpha")

    assert channel.last_request is not None
    assert SECRET not in channel.last_request.command_full
    assert SECRET not in channel.last_request.command_summary
    assert all(SECRET not in str(r.detail) for r in await sink.read_all())


async def test_the_request_is_recorded_before_it_is_delivered(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()
    seen: list[int] = []

    class CheckingChannel(SilentChannel):
        async def send_approval_request(self, request: ApprovalRequest) -> str:
            seen.append(len(await sink.read_all()))
            return await super().send_approval_request(request)

    store = ApprovalStore(ttl=timedelta(milliseconds=50))
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=CheckingChannel(),
        project="alpha-engine",
    )

    await ask(service, "git status")

    # An approval that reached a human before it reached the log is one that
    # could be acted on with no record that it was ever asked.
    assert seen == [1]


# --- the path breaks --------------------------------------------------------


async def test_nobody_answering_denies(tmp_path: Path) -> None:
    service, _, sink = build_service(
        tmp_path, channel=SilentChannel(), ttl=timedelta(milliseconds=50)
    )
    await sink.open()

    outcome = await ask(service)

    assert not outcome.allowed
    assert "expired" in outcome.reason.lower()


async def test_an_undeliverable_request_denies(tmp_path: Path) -> None:
    service, store, sink = build_service(tmp_path, channel=ExplodingChannel())
    await sink.open()

    outcome = await ask(service)

    assert not outcome.allowed
    assert "deliver" in outcome.reason.lower()
    # And it is closed out, not left open to be answered by somebody later.
    assert await store.list_open() == []


async def test_an_unwritable_audit_log_denies(tmp_path: Path) -> None:
    service, _, _ = build_service(tmp_path, audit=AuditLog([BrokenSink()]))

    outcome = await ask(service, "git status")

    # A decision nobody can account for afterwards is not one to act on.
    assert not outcome.allowed
    assert "audit" in outcome.reason.lower()


async def test_an_approval_that_cannot_be_recorded_is_not_honoured(tmp_path: Path) -> None:
    """The audit log works for the request and fails for the decision."""
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()

    class FailsOnResolution:
        async def open(self) -> None: ...
        async def close(self) -> None: ...

        async def write(self, record: AuditRecord) -> None:
            if record.action is AuditAction.APPROVAL_RESOLVED:
                raise OSError("disk full")
            await sink.write(record)

    store = ApprovalStore()
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([FailsOnResolution()]),
        registry=SessionRegistry(),
        channel=StubChannel(store, Decision.ALLOW),
        project="alpha-engine",
    )

    outcome = await ask(service, "git status")

    # An unrecorded denial is still a denial and stands. An unrecorded approval
    # is a command about to run with no trace of who agreed to it.
    assert not outcome.allowed
    assert "audit" in outcome.reason.lower()


async def test_an_unexpected_failure_denies_instead_of_raising(tmp_path: Path) -> None:
    class ExplodingPolicy(Policy):
        def classify(self, command: str, *, declared=None):
            raise RuntimeError("something nobody predicted")

    service, _, sink = build_service(tmp_path)
    service._policy = ExplodingPolicy()
    await sink.open()

    outcome = await ask(service)

    # request() must never raise. An exception escaping it becomes a 500, and a
    # hook that gets a 500 without a decision runs the command.
    assert not outcome.allowed
    assert "internal error" in outcome.reason.lower()


# --- naming the project a request came from -----------------------------------


@pytest.mark.parametrize(
    ("project_dir", "cwd", "expected"),
    [
        ("/Users/j/dev/agent-platform", "/Users/j/dev/agent-platform/sub", "agent-platform"),
        (None, "/Users/j/dev/agent-platform", "agent-platform"),
        ("/Users/j/dev/halyard-fleet/", None, "halyard-fleet"),
        # Nothing to go on, so the configured name is all that is left.
        (None, None, "configured-name"),
        ("", "", "configured-name"),
    ],
)
def test_the_project_is_named_from_the_path_it_came_from(
    project_dir: str | None, cwd: str | None, expected: str
) -> None:
    from halyard.core.service import project_name

    # CLAUDE_PROJECT_NAME is one value in one control plane. Gate a second
    # repository and its approvals would arrive wearing the first one's name —
    # found in real use, with a command from agent-platform arriving as
    # alpha-engine.
    assert project_name(project_dir, cwd, "configured-name") == expected


async def test_an_approval_card_names_the_calling_project(tmp_path: Path) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "ls", project_dir="/Users/j/dev/agent-platform")

    assert channel.last_request is not None
    # The service was configured with "alpha-engine".
    assert channel.last_request.project == "agent-platform"


async def test_a_request_without_a_project_dir_keeps_the_configured_name(
    tmp_path: Path,
) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "ls")

    assert channel.last_request is not None
    assert channel.last_request.project == "alpha-engine"
