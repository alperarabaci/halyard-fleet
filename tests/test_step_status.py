"""A workflow run's card saying what its step is doing, and the desk rewriting
it in place without ever reopening a card that has closed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from halyard.channels.telegram.desk import CardDesk
from halyard.channels.telegram.step_status import StepStatus, took

KEYBOARD = {"inline_keyboard": [[{"text": "Stop", "callback_data": "hc:flow:stop"}]]}
HEAD = "<b>level3</b> 1/2 · 🤝 <b>close_x</b> → <b>xnav</b>, running test-fast first…"


class Api:
    """Telegram, as far as a card needs it: what was sent and every edit."""

    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.hold: asyncio.Event | None = None

    async def send_message(self, chat, text, **_):
        return {"message_id": 7}

    async def edit_message_text(self, chat, message_id, text, *, reply_markup=None, **_):
        if self.hold is not None:
            await self.hold.wait()
        self.edits.append({"text": text, "reply_markup": reply_markup})

    async def edit_message_reply_markup(self, chat, message_id, *, reply_markup=None):
        self.edits.append({"text": None, "reply_markup": reply_markup})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def a_card() -> tuple[Api, CardDesk, object]:
    api = Api()
    desk = CardDesk(api, lambda: datetime(2026, 10, 25, tzinfo=UTC))
    card = await desk.open("-100", HEAD, KEYBOARD, kind="run", key="run-1")
    return api, desk, card


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (0, "under a minute"),
        (59, "under a minute"),
        (60, "1 min"),
        (720, "12 min"),
        (3600, "1 h 00 min"),
        (7500, "2 h 05 min"),
    ],
)
def test_the_time_is_said_in_minutes(seconds: float, said: str) -> None:
    assert took(seconds) == said


async def test_a_command_s_time_moves_on_the_card_even_when_it_prints_nothing() -> None:
    """A test run twelve minutes in, which has printed nothing, said nothing:
    from a phone it looked the same as one that had hung."""
    api, desk, card = await a_card()
    clock = Clock()
    status = StepStatus(desk, card, "close_x", clock=clock)

    await status.running("test-fast")
    clock.now += 12 * 60
    await status.show()

    assert api.edits[0]["text"] == f"{HEAD}\n\n⏳ close_x — running test-fast · under a minute"
    assert api.edits[-1]["text"] == f"{HEAD}\n\n⏳ close_x — running test-fast · 12 min"
    assert api.edits[-1]["reply_markup"] == KEYBOARD, "its buttons stay"


async def test_a_command_s_newest_line_is_under_its_time() -> None:
    api, desk, card = await a_card()
    status = StepStatus(desk, card, "close_x", clock=Clock())

    await status.running("test-fast")
    status.printed("collected 1520 items <fast>")
    await status.show()

    assert api.edits[-1]["text"].endswith(
        "running test-fast · under a minute\n<code>collected 1520 items &lt;fast&gt;</code>"
    )


async def test_inspections_are_counted_as_they_answer() -> None:
    api, desk, card = await a_card()
    clock = Clock()
    status = StepStatus(desk, card, "close_x", inspections=6, clock=clock)

    await status.inspecting()
    for _ in range(4):
        clock.now += 30
        await status.inspection_answered()
    await status.inspecting()

    assert "⏳ close_x — inspecting · 0 of 6 · under a minute" in api.edits[0]["text"]
    assert api.edits[-1]["text"].endswith("⏳ close_x — inspecting · 4 of 6 · 2 min"), (
        "counted from when they started, and another one starting changes nothing"
    )


async def test_on_its_way_and_then_waiting_for_its_agent() -> None:
    api, desk, card = await a_card()
    clock = Clock()
    status = StepStatus(desk, card, "close_x", clock=clock)

    await status.on_its_way("xnav")
    clock.now += 90
    await status.waiting("xnav")
    clock.now += 25 * 60
    await status.waiting("xnav")
    await status.show()

    texts = [edit["text"] for edit in api.edits]
    assert texts[0].endswith("⏳ close_x — on its way to xnav · under a minute")
    assert texts[-1].endswith("⏳ close_x — waiting for xnav · 25 min"), (
        "told again, the wait is the same wait"
    )


async def test_the_time_moves_by_itself_until_the_card_closes() -> None:
    api, desk, card = await a_card()
    clock = Clock()
    status = StepStatus(desk, card, "close_x", clock=clock, tick=0.01)
    await status.waiting("xnav")

    status.start()
    clock.now += 5 * 60
    for _ in range(100):
        if api.edits[-1]["text"].endswith("5 min"):
            break
        await asyncio.sleep(0.01)
    await desk.close_card(card, outcome="➡️ moved on")
    await asyncio.sleep(0.05)
    closed_at = len(api.edits)
    clock.now += 5 * 60
    await asyncio.sleep(0.05)

    assert api.edits[closed_at - 1]["text"].endswith("waiting for xnav · 5 min\n\n➡️ moved on"), (
        "closed as it last said"
    )
    assert len(api.edits) == closed_at, "nothing rewrites a card once it has closed"
    assert status._ticking is not None and status._ticking.done()


async def test_a_card_is_not_rewritten_when_nothing_on_it_changed() -> None:
    """Telegram refuses an edit that changes nothing."""
    api, desk, card = await a_card()
    status = StepStatus(desk, card, "close_x", clock=Clock())

    await status.waiting("xnav")
    await status.show()
    await status.show()

    assert len(api.edits) == 1


async def test_a_rewrite_on_its_way_finishes_before_the_card_closes() -> None:
    """Landed after the close, a rewrite would put the buttons back on a card
    that is over."""
    api, desk, card = await a_card()
    api.hold = asyncio.Event()

    rewriting = asyncio.create_task(desk.update(card, f"{HEAD}\n\n⏳ close_x — waiting for xnav"))
    await asyncio.sleep(0)
    closing = asyncio.create_task(desk.close_card(card, outcome="➡️ moved on"))
    await asyncio.sleep(0.01)
    api.hold.set()
    await asyncio.gather(rewriting, closing)

    assert [edit["reply_markup"] for edit in api.edits] == [KEYBOARD, None]
    assert api.edits[-1]["text"] == f"{HEAD}\n\n⏳ close_x — waiting for xnav\n\n➡️ moved on"
    assert await desk.update(card, "anything") is False


async def test_after_an_hour_at_it_the_time_moves_every_ten_minutes(monkeypatch) -> None:
    """A run can wait on its agent all night; a card rewritten every minute of
    it would be noise."""
    from halyard.channels.telegram import step_status

    _, desk, card = await a_card()
    clock = Clock()
    status = StepStatus(desk, card, "close_x", clock=clock)
    await status.waiting("xnav")
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def sleeping(seconds: float) -> None:
        slept.append(seconds)
        clock.now += 61 * 60
        if len(slept) == 2:
            await desk.close_card(card)
        await real_sleep(0)

    monkeypatch.setattr(step_status.asyncio, "sleep", sleeping)

    status.start()
    await asyncio.wait_for(status._ticking, timeout=5)

    assert slept == [step_status.TICK_SECONDS, step_status.SLOW_TICK_SECONDS]
