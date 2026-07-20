"""Tests for the approval store.

This is the module the whole fail-closed guarantee rests on, so the failure
paths are tested at least as hard as the happy one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from halyard.core.approvals import (
    AlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStore,
    Decision,
    InvalidNonceError,
    ResolutionReason,
    UnknownApprovalError,
)
from halyard.core.events import RiskLevel, Role

START = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)


class ManualClock:
    """A clock that only moves when a test says so."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def store(clock: ManualClock) -> ApprovalStore:
    return ApprovalStore(ttl=TTL, clock=clock)


async def open_request(store: ApprovalStore, **overrides: object):
    defaults = {
        "session_id": "session-1",
        "agent_id": "claude-code",
        "project": "alpha-engine",
        "tool": "Bash",
        "command_summary": "docker compose down postgres",
        "command_full": "docker compose down postgres",
        "risk": RiskLevel.HIGH,
    }
    return await store.create(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- creating ---------------------------------------------------------------


async def test_each_request_gets_a_distinct_id_and_nonce(store: ApprovalStore) -> None:
    requests = [await open_request(store) for _ in range(50)]

    assert len({r.request_id for r in requests}) == 50
    assert len({r.nonce for r in requests}) == 50
    assert all(r.request_id.startswith("req_") for r in requests)
    # secrets.token_urlsafe(16) is 22 characters. A short nonce is a guessable
    # nonce, and the nonce is the only thing protecting the callback.
    assert all(len(r.nonce) >= 22 for r in requests)


async def test_deadline_is_the_ttl_after_creation(store: ApprovalStore) -> None:
    request = await open_request(store)

    assert request.created_at == START
    assert request.expires_at == START + TTL


async def test_optional_context_is_carried(store: ApprovalStore) -> None:
    request = await open_request(store, role=Role.DRIVER, tool_use_id="toolu_1")

    assert request.role is Role.DRIVER
    assert request.tool_use_id == "toolu_1"
    # Nothing in a Phase 1 hook payload supplies a rationale.
    assert request.reason is None


async def test_open_requests_are_listed_oldest_first(store: ApprovalStore) -> None:
    first = await open_request(store)
    second = await open_request(store)
    await store.resolve(
        first.request_id, nonce=first.nonce, decision=Decision.ALLOW, decided_by="tg:1"
    )

    assert [r.request_id for r in await store.list_open()] == [second.request_id]


# --- deciding ---------------------------------------------------------------


async def test_wait_for_returns_the_decision_a_human_makes(store: ApprovalStore) -> None:
    request = await open_request(store)

    async def decide() -> None:
        await asyncio.sleep(0)  # let wait_for block first
        await store.resolve(
            request.request_id,
            nonce=request.nonce,
            decision=Decision.ALLOW,
            decided_by="tg:4242",
        )

    resolution, _ = await asyncio.gather(store.wait_for(request.request_id), decide())

    assert resolution.allowed
    assert resolution.decision is Decision.ALLOW
    assert resolution.reason is ResolutionReason.USER
    assert resolution.decided_by == "tg:4242"


async def test_a_denial_carries_an_actionable_note(store: ApprovalStore) -> None:
    request = await open_request(store)
    resolution = await store.resolve(
        request.request_id,
        nonce=request.nonce,
        decision=Decision.DENY,
        decided_by="tg:4242",
    )

    assert not resolution.allowed
    # Claude Code hands this string to the model verbatim, so it has to say
    # something the agent can act on.
    assert "tg:4242" in (resolution.note or "")


async def test_waiting_on_an_already_decided_request_returns_that_decision(
    store: ApprovalStore,
) -> None:
    request = await open_request(store)
    await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)

    assert (await store.wait_for(request.request_id)).allowed


# --- failing closed ---------------------------------------------------------


async def test_nobody_answering_denies(store: ApprovalStore, clock: ManualClock) -> None:
    request = await open_request(store)
    clock.advance(TTL.total_seconds() + 1)

    resolution = await store.wait_for(request.request_id)

    # Returned, not raised: a caller that must remember to catch an exception
    # in order to stay safe is a caller that will eventually forget.
    assert resolution.decision is Decision.DENY
    assert resolution.reason is ResolutionReason.TIMEOUT


async def test_nobody_answering_denies_on_a_real_timer() -> None:
    # The test above advances a manual clock, which reaches the deadline through
    # a zero-length wait. This one exercises the path that actually runs in
    # production: a live asyncio timer firing while the caller is blocked.
    store = ApprovalStore(ttl=timedelta(milliseconds=50))
    request = await open_request(store)

    resolution = await store.wait_for(request.request_id)

    assert resolution.decision is Decision.DENY
    assert resolution.reason is ResolutionReason.TIMEOUT


async def test_a_decision_that_wins_the_race_by_a_hair_is_honoured() -> None:
    # The deadline and a button press can land in the same instant. A human who
    # answered in time keeps their answer.
    store = ApprovalStore(ttl=timedelta(milliseconds=50))
    request = await open_request(store)

    async def decide() -> None:
        await asyncio.sleep(0.01)
        await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)

    resolution, _ = await asyncio.gather(store.wait_for(request.request_id), decide())

    assert resolution.allowed


async def test_shutdown_denies_everything_still_open(store: ApprovalStore) -> None:
    request = await open_request(store)

    await store.shutdown()
    resolution = await store.wait_for(request.request_id)

    assert resolution.decision is Decision.DENY
    assert resolution.reason is ResolutionReason.SHUTDOWN


async def test_a_decision_after_the_deadline_denies(
    store: ApprovalStore, clock: ManualClock
) -> None:
    request = await open_request(store)
    clock.advance(TTL.total_seconds() + 1)

    with pytest.raises(ApprovalExpiredError):
        await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)

    # And it is now closed as denied, so a second, luckier press cannot follow.
    resolution = await store.resolution_of(request.request_id)
    assert resolution is not None
    assert resolution.decision is Decision.DENY
    assert resolution.reason is ResolutionReason.EXPIRED


# --- replay and forgery -----------------------------------------------------


async def test_the_same_button_cannot_be_pressed_twice(store: ApprovalStore) -> None:
    request = await open_request(store)
    await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)

    with pytest.raises(AlreadyResolvedError):
        await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)


async def test_a_wrong_nonce_is_rejected(store: ApprovalStore) -> None:
    request = await open_request(store)

    with pytest.raises(InvalidNonceError):
        await store.resolve(request.request_id, nonce="not-the-nonce", decision=Decision.ALLOW)


async def test_a_wrong_nonce_does_not_burn_the_request(store: ApprovalStore) -> None:
    request = await open_request(store)
    with pytest.raises(InvalidNonceError):
        await store.resolve(request.request_id, nonce="wrong", decision=Decision.ALLOW)

    # A bad guess must not deny the request out from under the real approver.
    assert await store.resolution_of(request.request_id) is None
    assert (
        await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)
    ).allowed


async def test_a_nonce_from_another_request_is_rejected(store: ApprovalStore) -> None:
    first = await open_request(store)
    second = await open_request(store)

    with pytest.raises(InvalidNonceError):
        await store.resolve(second.request_id, nonce=first.nonce, decision=Decision.ALLOW)


async def test_unknown_requests_are_rejected(store: ApprovalStore) -> None:
    with pytest.raises(UnknownApprovalError):
        await store.resolve("req_nope", nonce="x", decision=Decision.ALLOW)
    with pytest.raises(UnknownApprovalError):
        await store.wait_for("req_nope")
    assert await store.get("req_nope") is None


async def test_only_one_of_many_simultaneous_presses_wins(store: ApprovalStore) -> None:
    request = await open_request(store)

    results = await asyncio.gather(
        *(
            store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)
            for _ in range(25)
        ),
        return_exceptions=True,
    )

    accepted = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, AlreadyResolvedError)]
    assert len(accepted) == 1
    assert len(rejected) == 24


# --- retries and retention --------------------------------------------------


async def test_a_retried_tool_call_reuses_its_open_request(store: ApprovalStore) -> None:
    first = await open_request(store, tool_use_id="toolu_1")
    second = await open_request(store, tool_use_id="toolu_1")

    # Otherwise a bridge retry produces a second card carrying a live nonce
    # that outlives the decision made on the first.
    assert second.request_id == first.request_id
    assert second.nonce == first.nonce
    assert len(await store.list_open()) == 1


async def test_a_retry_after_a_decision_opens_a_fresh_request(store: ApprovalStore) -> None:
    first = await open_request(store, tool_use_id="toolu_1")
    await store.resolve(first.request_id, nonce=first.nonce, decision=Decision.DENY)

    second = await open_request(store, tool_use_id="toolu_1")

    # A settled decision must never be silently reused as consent for a later call.
    assert second.request_id != first.request_id


async def test_requests_without_a_tool_use_id_are_never_merged(store: ApprovalStore) -> None:
    first = await open_request(store)
    second = await open_request(store)

    assert first.request_id != second.request_id


async def test_old_decisions_are_evicted_but_still_refuse(
    store: ApprovalStore, clock: ManualClock
) -> None:
    request = await open_request(store)
    await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)

    clock.advance(timedelta(hours=2).total_seconds())
    await open_request(store)  # any create sweeps stale records

    # Eviction changes the explanation from "already resolved" to "unknown",
    # never from refused to accepted.
    with pytest.raises(UnknownApprovalError):
        await store.resolve(request.request_id, nonce=request.nonce, decision=Decision.ALLOW)


async def test_open_requests_are_never_evicted(store: ApprovalStore, clock: ManualClock) -> None:
    request = await open_request(store)

    clock.advance(timedelta(hours=2).total_seconds())
    await open_request(store)

    assert await store.get(request.request_id) is not None
