"""Tests for the session registry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from halyard.core.events import Role
from halyard.core.registry import (
    SessionRegistry,
    SessionStatus,
    UnknownSessionError,
)

START = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


class FakeClock:
    """A clock that advances one second per reading, so ordering is exact."""

    def __init__(self) -> None:
        self._ticks = 0

    def __call__(self) -> datetime:
        now = START + timedelta(seconds=self._ticks)
        self._ticks += 1
        return now


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry(clock=FakeClock())


async def observe(registry: SessionRegistry, session_id: str = "session-1", **kwargs: object):
    defaults = {"agent_id": "claude-code", "project": "alpha-engine"}
    return await registry.observe(session_id=session_id, **{**defaults, **kwargs})  # type: ignore[arg-type]


async def test_first_sighting_creates_an_active_session(registry: SessionRegistry) -> None:
    session = await observe(registry, cwd="/repo", role=Role.DRIVER)

    assert session.session_id == "session-1"
    assert session.agent_id == "claude-code"
    assert session.project == "alpha-engine"
    assert session.cwd == "/repo"
    assert session.role is Role.DRIVER
    assert session.status is SessionStatus.ACTIVE
    assert session.first_seen_at == session.last_seen_at == START


async def test_repeat_sighting_refreshes_last_seen_but_keeps_first_seen(
    registry: SessionRegistry,
) -> None:
    first = await observe(registry)
    second = await observe(registry)

    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at > first.last_seen_at
    assert len(await registry.list_sessions()) == 1


async def test_repeat_sighting_does_not_erase_known_details(registry: SessionRegistry) -> None:
    await observe(registry, cwd="/repo", role=Role.NAVIGATOR)
    # A hook payload carries cwd but never a role, so a later, thinner
    # observation must not wipe what an earlier one established.
    session = await observe(registry)

    assert session.cwd == "/repo"
    assert session.role is Role.NAVIGATOR


async def test_repeat_sighting_fills_in_details_learned_later(registry: SessionRegistry) -> None:
    await observe(registry)
    session = await observe(registry, cwd="/repo", role=Role.DRIVER)

    assert session.cwd == "/repo"
    assert session.role is Role.DRIVER


async def test_unknown_session_reads_as_none(registry: SessionRegistry) -> None:
    assert await registry.get("never-seen") is None


async def test_sessions_are_listed_oldest_first(registry: SessionRegistry) -> None:
    await observe(registry, "session-a")
    await observe(registry, "session-b")
    await observe(registry, "session-a")

    assert [s.session_id for s in await registry.list_sessions()] == [
        "session-a",
        "session-b",
    ]


async def test_status_can_be_set_explicitly(registry: SessionRegistry) -> None:
    await observe(registry)
    session = await registry.set_status("session-1", SessionStatus.ENDED)

    assert session.status is SessionStatus.ENDED
    assert (await registry.get("session-1")).status is SessionStatus.ENDED


async def test_observing_an_ended_session_revives_it(registry: SessionRegistry) -> None:
    await observe(registry)
    await registry.set_status("session-1", SessionStatus.ENDED)

    assert (await observe(registry)).status is SessionStatus.ACTIVE


async def test_status_of_an_unknown_session_is_an_error(registry: SessionRegistry) -> None:
    # Inventing a placeholder entry here would hide a bug or a stale client.
    with pytest.raises(UnknownSessionError):
        await registry.set_status("never-seen", SessionStatus.ENDED)


async def test_concurrent_sightings_collapse_into_one_session(
    registry: SessionRegistry,
) -> None:
    # Claude Code dispatches independent tool calls in parallel, so several
    # hooks can arrive for one session at the same moment.
    await asyncio.gather(*(observe(registry, cwd="/repo") for _ in range(50)))

    sessions = await registry.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].first_seen_at == START
    assert sessions[0].last_seen_at > START
