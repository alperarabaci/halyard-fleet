"""The common event vocabulary every agent adapter translates into.

Core never learns what a Claude Code hook payload or an OpenCode stream event
looks like. Adapters perform that translation and emit `AgentEvent`; everything
above them — policy, audit, channels — speaks only this vocabulary. That is what
makes a second agent runtime an additive change rather than a rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """Every event the control plane will eventually understand.

    The full set is declared up front so adapters have a stable target to
    translate into, and so an adapter author can see what the system expects
    without waiting for each phase to land. Only the members listed in
    `PHASE_1_EVENT_TYPES` are actually produced today.
    """

    AGENT_STARTED = "agent.started"
    AGENT_MESSAGE = "agent.message"
    AGENT_QUESTION = "agent.question"
    PERMISSION_REQUESTED = "agent.permission_requested"
    PERMISSION_RESOLVED = "agent.permission_resolved"
    STATUS_CHANGED = "agent.status_changed"
    TOOL_STARTED = "agent.tool_started"
    TOOL_COMPLETED = "agent.tool_completed"
    TOOL_FAILED = "agent.tool_failed"
    STATE_SAVED = "agent.state_saved"
    HANDOFF_READY = "agent.handoff_ready"
    COMPLETED = "agent.completed"
    FAILED = "agent.failed"
    INTERRUPTED = "agent.interrupted"


#: The only event types Phase 1 emits. Everything else is declared but unused.
#: Kept as data rather than a comment so tests can assert the boundary holds and
#: nobody widens the scope of Phase 1 by accident.
PHASE_1_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.PERMISSION_REQUESTED, EventType.PERMISSION_RESOLVED}
)


class Role(StrEnum):
    """The role a session plays in a navigator/driver workflow.

    Unset for a plain single-session setup, which is all Phase 1 supports.
    """

    NAVIGATOR = "navigator"
    DRIVER = "driver"
    REVIEWER = "reviewer"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_event_id() -> str:
    return f"evt_{uuid4().hex}"


class AgentEvent(BaseModel):
    """Something an agent did, in the control plane's own words.

    Events are immutable. They are facts about the past, and they feed an
    append-only audit log — a mutable event would let a record be rewritten
    after it was already observed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: EventType
    agent_id: str
    session_id: str
    project: str
    payload: dict[str, Any] = Field(default_factory=dict)
    role: Role | None = None
    event_id: str = Field(default_factory=_new_event_id)
    timestamp: datetime = Field(default_factory=_now)
