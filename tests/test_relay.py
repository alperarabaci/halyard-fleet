"""Tests for relaying an agent's output to a channel.

The rule here is the inverse of the approval path, so most of these check that
nothing is enforced: a relay that fails reports it and gets out of the way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from halyard.core.audit import AuditAction, AuditLog, AuditRecord, JsonlAuditSink
from halyard.core.events import Role
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry
from halyard.core.service import MessageRelay

SECRET = "hunter2SuperSecretValue"


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.routes: list[tuple[str | None, str | None, object]] = []
        self.documents: list[tuple[str, str, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_approval_request(self, request) -> str: ...

    async def send_message(
        self,
        session_id: str,
        text: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str:
        self.messages.append((session_id, text))
        self.routes.append((agent_id, session_name, role))
        return "msg-1"

    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str:
        self.documents.append((session_id, content, title))
        return "doc-1"


class BrokenChannel(RecordingChannel):
    async def send_message(
        self,
        session_id: str,
        text: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str:
        raise ConnectionError("telegram unreachable")

    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str:
        raise ConnectionError("telegram unreachable")


def build(tmp_path: Path, channel=None, audit: AuditLog | None = None):
    channel = channel or RecordingChannel()
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    registry = SessionRegistry()
    relay = MessageRelay(
        redactor=Redactor(),
        registry=registry,
        audit=audit or AuditLog([sink]),
        channel=channel,
        project="alpha-engine",
    )
    return relay, channel, sink, registry


async def say(relay: MessageRelay, text: str = "Done. Tests pass.", **kwargs) -> bool:
    defaults = {"session_id": "session-1", "agent_id": "claude-code"}
    return await relay.relay(text=text, **{**defaults, **kwargs})


# --- the happy path ---------------------------------------------------------


async def test_a_reply_reaches_the_channel(tmp_path: Path) -> None:
    relay, channel, sink, _ = build(tmp_path)
    await sink.open()

    assert await say(relay) is True
    assert channel.messages == [("session-1", "Done. Tests pass.")]


async def test_a_reply_keeps_its_runtime_and_session_route(tmp_path: Path) -> None:
    relay, channel, sink, _ = build(tmp_path)
    await sink.open()

    await say(
        relay,
        agent_id="codex",
        session_name="alpha-engine-xdriver",
        role=Role.DRIVER,
    )

    assert channel.routes == [("codex", "alpha-engine-xdriver", Role.DRIVER)]


async def test_the_session_is_observed(tmp_path: Path) -> None:
    relay, _, sink, registry = build(tmp_path)
    await sink.open()

    await say(relay, cwd="/repo")

    session = await registry.get("session-1")
    assert session is not None
    assert session.cwd == "/repo"


async def test_a_long_reply_still_arrives_as_a_message(tmp_path: Path) -> None:
    relay, channel, sink, _ = build(tmp_path)
    await sink.open()

    await say(relay, "x" * 9000)

    # A reply arriving as a file has to be tapped, downloaded and opened, and
    # reading it where it lands is the entire point. The channel splits it.
    assert channel.documents == []
    assert len(channel.messages) == 1


# --- secrets ----------------------------------------------------------------


async def test_a_secret_is_masked_before_it_leaves(tmp_path: Path) -> None:
    relay, channel, sink, _ = build(tmp_path)
    await sink.open()

    await say(relay, f"I ran psql postgres://alper:{SECRET}@db/alpha and it worked")

    # An agent quoting a command it just ran can quote a credential with it, and
    # the text is about to land on somebody else's servers.
    sent = channel.messages[0][1]
    assert SECRET not in sent
    assert "***" in sent


# --- what gets written down -------------------------------------------------


async def test_the_audit_records_that_a_message_went_out(tmp_path: Path) -> None:
    relay, _, sink, _ = build(tmp_path)
    await sink.open()

    await say(relay)

    record = (await sink.read_all())[0]
    assert record.action is AuditAction.AGENT_MESSAGE
    assert record.session_id == "session-1"
    assert record.detail["delivered"] is True
    assert record.detail["length"] == len("Done. Tests pass.")


async def test_the_audit_does_not_keep_the_text(tmp_path: Path) -> None:
    relay, _, sink, _ = build(tmp_path)
    await sink.open()

    await say(relay, "a distinctive sentence nobody should find in the audit log")

    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    # This log is the permanent record of decisions. An assistant's conversation
    # is not one, and copying it here would grow the record without bound with
    # content nobody reviewed.
    assert "distinctive sentence" not in written


async def test_a_failure_to_deliver_is_recorded_as_such(tmp_path: Path) -> None:
    relay, _, sink, _ = build(tmp_path, channel=BrokenChannel())
    await sink.open()

    assert await say(relay) is False

    assert (await sink.read_all())[0].detail["delivered"] is False


# --- failing without enforcing ----------------------------------------------


async def test_an_unreachable_channel_reports_rather_than_raises(tmp_path: Path) -> None:
    relay, _, sink, _ = build(tmp_path, channel=BrokenChannel())
    await sink.open()

    # The inverse of the approval path. A lost chat message is not worth
    # stalling the agent's turn over, so this reports and gets out of the way.
    assert await say(relay) is False


async def test_an_unwritable_audit_log_does_not_lose_the_message(tmp_path: Path) -> None:
    class BrokenSink:
        async def open(self) -> None: ...
        async def close(self) -> None: ...
        async def write(self, record: AuditRecord) -> None:
            raise OSError("disk full")

    relay, channel, _, _ = build(tmp_path, audit=AuditLog([BrokenSink()]))

    # An unrecorded message is not an unrecorded decision: nothing is waiting on
    # it and there is nothing to undo.
    assert await say(relay) is True
    assert len(channel.messages) == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
async def test_an_empty_reply_is_still_handled_without_raising(tmp_path: Path, text: str) -> None:
    relay, channel, sink, _ = build(tmp_path)
    await sink.open()

    # The bridge filters these out, but the service must not depend on that.
    assert await say(relay, text) is True
    assert len(channel.messages) == 1


async def test_relaying_survives_a_channel_that_returns_nonsense(tmp_path: Path) -> None:
    class WeirdChannel(RecordingChannel):
        async def send_message(
            self,
            session_id: str,
            text: str,
            role=None,
            *,
            agent_id=None,
            session_name=None,
        ):
            return None

    relay, _, sink, _ = build(tmp_path, channel=WeirdChannel())
    await sink.open()

    assert await say(relay) is True


async def test_the_audit_line_is_valid_json(tmp_path: Path) -> None:
    relay, _, sink, _ = build(tmp_path)
    await sink.open()

    await say(relay)

    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["action"] == "agent.message"
