"""Tests for the common agent event vocabulary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from halyard.core.events import PHASE_1_EVENT_TYPES, AgentEvent, EventType, Role


def make_event(**overrides: object) -> AgentEvent:
    defaults = {
        "event_type": EventType.PERMISSION_REQUESTED,
        "agent_id": "claude-code",
        "session_id": "session-1",
        "project": "alpha-engine",
    }
    return AgentEvent(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_event_ids_are_unique_and_prefixed() -> None:
    ids = {make_event().event_id for _ in range(100)}
    assert len(ids) == 100
    assert all(event_id.startswith("evt_") for event_id in ids)


def test_timestamp_defaults_to_an_aware_utc_instant() -> None:
    event = make_event()
    # A naive timestamp would compare and serialize ambiguously once the audit
    # log and Telegram cards start rendering expiry countdowns.
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset() == UTC.utcoffset(None)


def test_events_are_immutable() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.session_id = "someone-elses-session"  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    # A typo in an adapter must fail loudly rather than quietly dropping data
    # that core would then never see.
    with pytest.raises(ValidationError):
        make_event(sesion_id="typo")


def test_role_is_optional() -> None:
    assert make_event().role is None
    assert make_event(role=Role.DRIVER).role is Role.DRIVER


def test_payload_defaults_are_not_shared_between_events() -> None:
    first = make_event()
    second = make_event()
    first.payload["tool"] = "Bash"
    assert second.payload == {}


def test_event_type_serializes_to_its_dotted_name() -> None:
    event = make_event(event_type=EventType.PERMISSION_RESOLVED, role=Role.NAVIGATOR)
    dumped = event.model_dump(mode="json")
    assert dumped["event_type"] == "agent.permission_resolved"
    assert dumped["role"] == "navigator"


def test_round_trips_through_json() -> None:
    event = make_event(
        payload={"tool": "Bash", "command_summary": "git status"},
        role=Role.DRIVER,
        timestamp=datetime(2026, 7, 20, 18, 30, tzinfo=UTC),
    )
    assert AgentEvent.model_validate_json(event.model_dump_json()) == event


def test_phase_1_produces_only_permission_events() -> None:
    # A guard, not a tautology: widening Phase 1's scope should require
    # deliberately editing this expectation.
    produced = set(PHASE_1_EVENT_TYPES)
    assert produced == {EventType.PERMISSION_REQUESTED, EventType.PERMISSION_RESOLVED}
    assert produced.issubset(EventType)
    assert produced != set(EventType)
