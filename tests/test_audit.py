"""Tests for the append-only audit log."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from halyard.core.approvals import (
    ApprovalRequest,
    ApprovalResolution,
    Decision,
    ResolutionReason,
)
from halyard.core.audit import (
    AuditAction,
    AuditLog,
    AuditRecord,
    AuditWriteError,
    JsonlAuditSink,
    SqliteAuditSink,
    approval_requested,
    approval_resolved,
    bridge_error,
    invalid_nonce,
    replayed_callback,
    unauthorized_callback,
)
from halyard.core.events import RiskLevel, Role

NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


def a_request(**overrides: object) -> ApprovalRequest:
    defaults = {
        "request_id": "req_1",
        "nonce": "nonce-1",
        "session_id": "session-1",
        "agent_id": "claude-code",
        "project": "alpha-engine",
        "tool": "Bash",
        "command_summary": "docker compose down postgres",
        "command_full": "docker compose down postgres",
        "risk": RiskLevel.HIGH,
        "role": Role.DRIVER,
        "tool_use_id": "toolu_1",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    return ApprovalRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def a_resolution(**overrides: object) -> ApprovalResolution:
    defaults = {
        "request_id": "req_1",
        "decision": Decision.DENY,
        "reason": ResolutionReason.TIMEOUT,
        "decided_at": NOW + timedelta(minutes=5),
        "note": "Denied: nobody answered.",
    }
    return ApprovalResolution(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture
async def sinks(tmp_path: Path):
    jsonl = JsonlAuditSink(tmp_path / "audit.jsonl")
    sqlite = SqliteAuditSink(tmp_path / "halyard.db")
    log = AuditLog([jsonl, sqlite])
    await log.open()
    try:
        yield log, jsonl, sqlite
    finally:
        await log.close()


# --- fanning out ------------------------------------------------------------


async def test_a_record_reaches_every_sink(sinks) -> None:
    log, jsonl, sqlite = sinks
    await log.record(approval_requested(a_request(), now=NOW))

    from_file = await jsonl.read_all()
    from_db = await sqlite.read_all()

    assert len(from_file) == len(from_db) == 1
    assert from_file[0] == from_db[0]
    assert from_file[0].action is AuditAction.APPROVAL_REQUESTED


async def test_records_keep_the_order_they_were_written(sinks) -> None:
    log, jsonl, sqlite = sinks
    request = a_request()
    await log.record(approval_requested(request, now=NOW))
    await log.record(approval_resolved(request, a_resolution()))

    for records in (await jsonl.read_all(), await sqlite.read_all()):
        assert [r.action for r in records] == [
            AuditAction.APPROVAL_REQUESTED,
            AuditAction.APPROVAL_RESOLVED,
        ]


async def test_a_failing_sink_is_reported_without_losing_the_others(tmp_path: Path) -> None:
    class BrokenSink:
        async def open(self) -> None: ...
        async def close(self) -> None: ...
        async def write(self, record: AuditRecord) -> None:
            raise OSError("disk full")

    jsonl = JsonlAuditSink(tmp_path / "audit.jsonl")
    log = AuditLog([BrokenSink(), jsonl])
    await log.open()

    with pytest.raises(AuditWriteError) as caught:
        await log.record(approval_requested(a_request(), now=NOW))

    # The sinks are redundant. Giving up on the first failure would discard the
    # redundancy at exactly the moment it matters.
    assert len(await jsonl.read_all()) == 1
    assert isinstance(caught.value.failures[0], OSError)
    await log.close()


# --- append-only ------------------------------------------------------------


async def test_the_database_refuses_updates(sinks, tmp_path: Path) -> None:
    log, _, _ = sinks
    await log.record(approval_requested(a_request(), now=NOW))

    async with aiosqlite.connect(tmp_path / "halyard.db") as db:
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("UPDATE audit_log SET actor = 'someone else'")


async def test_the_database_refuses_deletes(sinks, tmp_path: Path) -> None:
    log, _, _ = sinks
    await log.record(approval_requested(a_request(), now=NOW))

    async with aiosqlite.connect(tmp_path / "halyard.db") as db:
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("DELETE FROM audit_log")


async def test_sequence_numbers_are_never_reused(sinks, tmp_path: Path) -> None:
    log, _, _ = sinks
    for _ in range(3):
        await log.record(bridge_error(message="boom"))

    async with aiosqlite.connect(tmp_path / "halyard.db") as db:
        cursor = await db.execute("SELECT sequence FROM audit_log ORDER BY sequence")
        sequences = [row[0] for row in await cursor.fetchall()]

    # AUTOINCREMENT rather than a bare rowid, so a removed row would leave a
    # visible gap instead of being silently backfilled by the next insert.
    assert sequences == [1, 2, 3]


async def test_the_file_is_appended_to_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    first = JsonlAuditSink(path)
    await first.open()
    await first.write(bridge_error(message="before restart"))
    await first.close()

    second = JsonlAuditSink(path)
    await second.open()
    await second.write(bridge_error(message="after restart"))
    await second.close()

    # Truncating on startup would make "no record of it" the cheapest way to
    # erase an inconvenient decision.
    records = await JsonlAuditSink(path).read_all()
    assert [r.detail["message"] for r in records] == ["before restart", "after restart"]


# --- the file a human reads -------------------------------------------------


async def test_each_line_is_one_readable_json_object(sinks, tmp_path: Path) -> None:
    log, _, _ = sinks
    request = a_request()
    await log.record(approval_requested(request, now=NOW))
    await log.record(approval_resolved(request, a_resolution()))

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["action"] == "approval.requested"
    assert parsed[0]["detail"]["command"] == "docker compose down postgres"
    assert parsed[1]["detail"]["decision"] == "deny"


async def test_both_sinks_spell_a_timestamp_the_same_way(sinks, tmp_path: Path) -> None:
    log, _, _ = sinks
    await log.record(approval_requested(a_request(), now=NOW))

    from_file = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    async with aiosqlite.connect(tmp_path / "halyard.db") as db:
        cursor = await db.execute("SELECT recorded_at FROM audit_log")
        from_db = (await cursor.fetchone())[0]

    # Two copies of one instant written by two code paths will drift apart the
    # moment anyone hand-rolls the conversion in only one of them.
    assert from_file["recorded_at"] == from_db
    assert datetime.fromisoformat(from_db) == NOW


async def test_concurrent_writes_do_not_interleave(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()

    await asyncio.gather(*(sink.write(bridge_error(message=f"error {i}")) for i in range(50)))
    await sink.close()

    # Two writers splicing halves of two records into one line would leave the
    # log unparseable, which is the same as not having one.
    records = await JsonlAuditSink(tmp_path / "audit.jsonl").read_all()
    assert len(records) == 50
    assert {r.detail["message"] for r in records} == {f"error {i}" for i in range(50)}


# --- what gets recorded -----------------------------------------------------


async def test_a_request_record_carries_the_command_and_its_risk() -> None:
    record = approval_requested(a_request(), now=NOW)

    assert record.action is AuditAction.APPROVAL_REQUESTED
    assert record.request_id == "req_1"
    assert record.session_id == "session-1"
    assert record.project == "alpha-engine"
    assert record.detail["command"] == "docker compose down postgres"
    assert record.detail["risk"] == "high"
    assert record.detail["role"] == "driver"
    assert record.detail["tool_use_id"] == "toolu_1"


async def test_a_decision_record_stands_on_its_own() -> None:
    record = approval_resolved(a_request(), a_resolution())

    assert record.action is AuditAction.APPROVAL_RESOLVED
    assert record.detail["decision"] == "deny"
    assert record.detail["reason"] == "timeout"
    # Repeated from the request record so a reader scanning decisions never has
    # to join back to another line to see what was decided.
    assert record.detail["command"] == "docker compose down postgres"


async def test_an_unattended_decision_is_attributed_to_the_system() -> None:
    assert approval_resolved(a_request(), a_resolution()).actor == "system"


async def test_a_human_decision_names_who_made_it() -> None:
    record = approval_resolved(
        a_request(),
        a_resolution(
            decision=Decision.ALLOW,
            reason=ResolutionReason.USER,
            decided_by="tg:4242",
        ),
    )
    assert record.actor == "tg:4242"


async def test_rejected_callbacks_are_recorded_with_their_actor() -> None:
    assert unauthorized_callback(actor="tg:9999", channel="telegram").action is (
        AuditAction.UNAUTHORIZED_CALLBACK
    )
    assert invalid_nonce(actor="tg:9999", request_id="req_1").action is (AuditAction.INVALID_NONCE)
    assert replayed_callback(actor="tg:4242", request_id="req_1").action is (
        AuditAction.REPLAYED_CALLBACK
    )
    assert unauthorized_callback(actor="tg:9999", channel="telegram").actor == "tg:9999"


async def test_bridge_failures_are_attributed_to_the_bridge() -> None:
    record = bridge_error(message="control plane unreachable", session_id="session-1")

    assert record.action is AuditAction.BRIDGE_ERROR
    assert record.actor == "bridge"
    assert record.detail["message"] == "control plane unreachable"


# --- lifecycle --------------------------------------------------------------


async def test_writing_before_opening_is_an_error(tmp_path: Path) -> None:
    for sink in (JsonlAuditSink(tmp_path / "a.jsonl"), SqliteAuditSink(tmp_path / "a.db")):
        with pytest.raises(RuntimeError, match="open"):
            await sink.write(bridge_error(message="too early"))
