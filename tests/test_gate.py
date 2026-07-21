"""Tests for the pause switch.

The one thing every test here is really checking: pausing hands the question
back to the terminal, and never answers it. A switch that granted commands
would be automatic approval with a friendlier name, which this project does
not do.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_service import SilentChannel, ask, build_service

from halyard.core.audit import AuditAction, AuditLog, JsonlAuditSink
from halyard.core.gate import Gate
from halyard.core.service import BridgeDecision

START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


# --- the switch itself --------------------------------------------------------


async def test_it_starts_open() -> None:
    assert Gate().paused is False


async def test_pausing_closes_it_and_says_it_changed() -> None:
    gate = Gate(clock=ManualClock())

    state, changed = await gate.pause("tg:4242")

    assert state.paused is True
    assert state.changed_by == "tg:4242"
    assert changed is True
    assert gate.paused is True


async def test_pausing_twice_is_not_an_error() -> None:
    gate = Gate()
    await gate.pause("tg:4242")

    state, changed = await gate.pause("tg:4242")

    # The point of the switch is to be reachable when things are going wrong.
    # An error at that moment reads as a failure.
    assert state.paused is True
    assert changed is False


async def test_resuming_reopens_it() -> None:
    gate = Gate()
    await gate.pause("tg:4242")

    state, changed = await gate.resume("tg:1337")

    assert state.paused is False
    assert changed is True
    assert state.changed_by == "tg:1337"


async def test_resuming_something_already_running_is_not_an_error() -> None:
    state, changed = await Gate().resume("tg:4242")

    assert state.paused is False
    assert changed is False


async def test_concurrent_toggles_leave_it_in_one_state() -> None:
    gate = Gate()

    results = await asyncio.gather(*(gate.pause("tg:4242") for _ in range(20)))

    # Exactly one call did the changing; the rest confirmed.
    assert sum(1 for _, changed in results if changed) == 1
    assert gate.paused is True


# --- what a paused gate does to an approval ----------------------------------


async def test_a_paused_gate_defers_instead_of_deciding(tmp_path: Path) -> None:
    gate = Gate()
    await gate.pause("tg:4242")
    channel = SilentChannel()
    service, store, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    service._gate = gate
    await sink.open()

    outcome = await ask(service, "rm -rf /var/lib/alpha")

    # Not allowed, not denied. Claude Code falls back to its own prompt, which
    # is where the question lived before Halyard existed.
    assert outcome.decision is BridgeDecision.DEFER
    assert outcome.allowed is False
    # And nothing was created, asked, or recorded — there is no approval.
    assert channel.last_request is None
    assert await store.list_open() == []
    assert await sink.read_all() == []


async def test_resuming_starts_asking_again(tmp_path: Path) -> None:
    gate = Gate()
    await gate.pause("tg:4242")
    service, _, sink = build_service(tmp_path)
    service._gate = gate
    await sink.open()
    assert (await ask(service, "git status")).decision is BridgeDecision.DEFER

    await gate.resume("tg:4242")

    assert (await ask(service, "git status")).decision is BridgeDecision.ALLOW


async def test_deferring_is_never_allowing(tmp_path: Path) -> None:
    gate = Gate()
    await gate.pause("tg:4242")
    service, _, sink = build_service(tmp_path)
    service._gate = gate
    await sink.open()

    outcome = await ask(service, "rm -rf /")

    # The property this whole feature has to keep. A pause that granted
    # commands would be automatic approval under another name.
    assert outcome.decision is not BridgeDecision.ALLOW
    assert not outcome.allowed


# --- what gets written down ---------------------------------------------------


async def test_changing_the_gate_is_recorded_with_who_did_it(tmp_path: Path) -> None:
    from halyard.core.audit import gate_changed

    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()

    await audit.record(gate_changed(paused=True, actor="tg:4242", project="alpha-engine"))
    await audit.record(gate_changed(paused=False, actor="tg:4242", project="alpha-engine"))

    records = await sink.read_all()
    assert [r.action for r in records] == [AuditAction.GATE_PAUSED, AuditAction.GATE_RESUMED]
    assert all(r.actor == "tg:4242" for r in records)
    await audit.close()


@pytest.mark.parametrize("paused", [True, False])
def test_the_record_says_which_way_it_went(paused: bool) -> None:
    from halyard.core.audit import gate_changed

    record = gate_changed(paused=paused, actor="tg:4242", project="p")

    assert record.detail["paused"] is paused


# --- pausing silences the phone in both directions ----------------------------


async def test_a_paused_gate_does_not_relay_replies(tmp_path: Path) -> None:
    from test_relay import RecordingChannel

    from halyard.core.redaction import Redactor
    from halyard.core.registry import SessionRegistry
    from halyard.core.service import MessageRelay

    gate = Gate()
    await gate.pause("tg:4242")
    channel = RecordingChannel()
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()
    relay = MessageRelay(
        redactor=Redactor(),
        registry=SessionRegistry(),
        audit=AuditLog([sink]),
        channel=channel,
        project="alpha-engine",
        gate=gate,
    )

    delivered = await relay.relay(session_id="s", agent_id="claude-code", text="Done.")

    # Pausing means the phone is off, not that approvals alone stop. Someone
    # who took the decisions back to the keyboard does not want replies buzzing
    # on a device they are not looking at.
    assert delivered is False
    assert channel.messages == []
    assert await sink.read_all() == []


async def test_resuming_starts_relaying_replies_again(tmp_path: Path) -> None:
    from test_relay import RecordingChannel

    from halyard.core.redaction import Redactor
    from halyard.core.registry import SessionRegistry
    from halyard.core.service import MessageRelay

    gate = Gate()
    await gate.pause("tg:4242")
    channel = RecordingChannel()
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()
    relay = MessageRelay(
        redactor=Redactor(),
        registry=SessionRegistry(),
        audit=AuditLog([sink]),
        channel=channel,
        project="alpha-engine",
        gate=gate,
    )
    await relay.relay(session_id="s", agent_id="claude-code", text="while paused")

    await gate.resume("tg:4242")
    await relay.relay(session_id="s", agent_id="claude-code", text="after resuming")

    assert [text for _, text in channel.messages] == ["after resuming"]


async def test_commands_still_work_while_paused(tmp_path: Path) -> None:
    from test_telegram import gated, typed

    channel, api, gate, _ = await gated(tmp_path)
    await channel._handle_message(typed("/pause"))
    api.sent.clear()

    await channel._handle_message(typed("/status"))
    await channel._handle_message(typed("/resume"))

    # The switch has to be reachable from the place it silenced, or pausing
    # would be a one-way door.
    assert len(api.sent) == 2
    assert gate.paused is False
