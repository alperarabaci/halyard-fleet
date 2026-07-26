"""Tests for the question store.

The sibling of the approval store, and the failure paths matter as much here —
but the safe direction is inverted. An approval left unanswered denies; a
question left unanswered is *unanswered*, which sends the choice back to the
terminal. The tests below hold that line: no path invents an answer nobody gave.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from halyard.core.approvals import ResolutionReason
from halyard.core.questions import (
    AlreadyAnsweredError,
    Choice,
    InvalidNonceError,
    QuestionExpiredError,
    QuestionStore,
    UnknownQuestionError,
)

START = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)


class ManualClock:
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
def store(clock: ManualClock) -> QuestionStore:
    return QuestionStore(ttl=TTL, clock=clock)


async def open_question(store: QuestionStore, **overrides: object):
    defaults = {
        "session_id": "session-1",
        "agent_id": "claude-code",
        "project": "alpha-engine",
        "question": "Which is your favorite color?",
        "options": [Choice(label="Red"), Choice(label="Blue")],
    }
    defaults.update(overrides)
    return await store.create(**defaults)


async def test_a_chosen_option_comes_back_as_its_label(store: QuestionStore) -> None:
    request = await open_question(store)

    resolution = await store.answer(
        request.request_id, nonce=request.nonce, answer="Red", decided_by="tg:4242"
    )

    assert resolution.answer == "Red"
    assert resolution.answered is True
    assert resolution.reason is ResolutionReason.USER
    assert resolution.decided_by == "tg:4242"


async def test_a_typed_answer_is_kept_verbatim(store: QuestionStore) -> None:
    """The "Other" path: whatever somebody types instead of choosing is the
    answer, unchanged."""
    request = await open_question(store)

    resolution = await store.answer(
        request.request_id, nonce=request.nonce, answer="actually, use teal"
    )

    assert resolution.answer == "actually, use teal"


async def test_waiting_returns_the_answer(store: QuestionStore) -> None:
    request = await open_question(store)

    async def choose() -> None:
        await asyncio.sleep(0)
        await store.answer(request.request_id, nonce=request.nonce, answer="Blue")

    task = asyncio.create_task(choose())
    resolution = await store.wait_for(request.request_id)
    await task

    assert resolution.answer == "Blue"


async def test_a_deadline_leaves_it_unanswered_rather_than_denied(
    store: QuestionStore, clock: ManualClock
) -> None:
    """The whole reason this is a separate store. There is no denial here — the
    terminal picker gets the choice, which needs `answer` to be None, not a
    third decision value smuggled into the approval enum."""
    request = await open_question(store)
    clock.advance(TTL.total_seconds() + 1)

    resolution = await store.wait_for(request.request_id)

    assert resolution.answer is None
    assert resolution.answered is False
    assert resolution.reason is ResolutionReason.TIMEOUT


async def test_a_late_answer_is_refused(store: QuestionStore, clock: ManualClock) -> None:
    request = await open_question(store)
    clock.advance(TTL.total_seconds() + 1)

    with pytest.raises(QuestionExpiredError):
        await store.answer(request.request_id, nonce=request.nonce, answer="Red")


async def test_answering_twice_is_refused(store: QuestionStore) -> None:
    request = await open_question(store)
    await store.answer(request.request_id, nonce=request.nonce, answer="Red")

    with pytest.raises(AlreadyAnsweredError):
        await store.answer(request.request_id, nonce=request.nonce, answer="Blue")


async def test_a_bad_nonce_is_refused(store: QuestionStore) -> None:
    request = await open_question(store)

    with pytest.raises(InvalidNonceError):
        await store.answer(request.request_id, nonce="not-the-nonce", answer="Red")


async def test_an_unknown_question_is_refused(store: QuestionStore) -> None:
    with pytest.raises(UnknownQuestionError):
        await store.answer("ask_nope", nonce="x", answer="Red")


async def test_a_retried_call_returns_the_same_open_question(store: QuestionStore) -> None:
    """A bridge that retried after the server had already handled it must not
    put two cards on the phone for one question."""
    first = await open_question(store, tool_use_id="toolu_ask")
    second = await open_question(store, tool_use_id="toolu_ask")

    assert first.request_id == second.request_id


async def test_shutdown_leaves_open_questions_unanswered(store: QuestionStore) -> None:
    """A bridge blocked on us is told to fall back to the terminal, not left to
    wait out its own timeout."""
    request = await open_question(store)

    await store.shutdown()
    resolution = await store.wait_for(request.request_id)

    assert resolution.answer is None
    assert resolution.reason is ResolutionReason.SHUTDOWN


async def test_a_choice_landing_with_the_deadline_keeps_the_choice(
    store: QuestionStore, clock: ManualClock
) -> None:
    """The deadline and a press can reach the lock in the same instant. Whoever
    is first wins, and a person who answered in time keeps their answer."""
    request = await open_question(store)
    resolution = await store.answer(request.request_id, nonce=request.nonce, answer="Red")

    # give_up now finds it already answered and returns that, rather than
    # overwriting a real choice with "nobody answered".
    later = await store.give_up(request.request_id, reason=ResolutionReason.TIMEOUT)

    assert later.answer == "Red"
    assert resolution.answer == "Red"
