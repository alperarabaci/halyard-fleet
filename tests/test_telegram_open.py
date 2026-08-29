"""Tests for `/open` — starting an agent that is not running.

Its own file, because opening an application and committing a branch have
nothing to do with each other beyond arriving through the same chat.

`desktop` is doubled throughout. Letting these run for real would open
applications on whoever's machine is running the suite.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from halyard.applications import desktop
from halyard.channels.telegram import cards
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.core.approvals import ApprovalStore
from halyard.core.audit import AuditLog, JsonlAuditSink

CHAT = "-100777"
APPROVER = "4242"


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next = 100

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def set_my_commands(self, commands) -> None: ...

    async def send_message(self, chat_id, text, *, reply_markup=None, **kwargs) -> dict:
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id, *, text=None): ...


@pytest.fixture
async def wired(tmp_path: Path):
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeApi()
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        poll_retry_seconds=0.01,
    )
    try:
        yield channel, api
    finally:
        await audit.close()


def typed(text: str, *, user: str = APPROVER) -> dict:
    return {"message_id": 1, "from": {"id": int(user)}, "chat": {"id": CHAT}, "text": text}


@pytest.fixture
def on_a_mac(monkeypatch):
    monkeypatch.setattr(desktop, "available", lambda: True)


def closed(monkeypatch) -> list[str]:
    """Everything installed and nothing running, recording what gets opened."""
    return standing(monkeypatch, running=False, on_screen=False)


def standing(monkeypatch, *, running: bool, on_screen: bool) -> list[str]:
    """Every application in one state, recording what gets opened."""
    opened: list[str] = []
    monkeypatch.setattr(
        desktop, "status", lambda app: desktop.Status(Path("/x.app"), running, on_screen)
    )
    monkeypatch.setattr(desktop, "open_", lambda app: opened.append(app.bundle_id) or True)
    return opened


async def test_open_starts_an_application_that_is_closed(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    opened = closed(monkeypatch)

    await channel._handle_message(typed("/open codex"))

    assert opened == ["com.openai.codex"]
    assert "Asked macOS to open" in api.sent[-1]["text"]


async def test_open_takes_the_name_somebody_actually_types(monkeypatch, on_a_mac, wired) -> None:
    """Gemini is what Antigravity is to the person typing."""
    channel, _ = wired
    opened = closed(monkeypatch)

    await channel._handle_message(typed("/open gemini"))

    assert opened == ["com.google.antigravity"]


async def test_open_does_not_relaunch_something_on_screen(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(Path("/x.app"), True, True))

    def explode(app):
        raise AssertionError("should not open something already on screen")

    monkeypatch.setattr(desktop, "open_", explode)
    await channel._handle_message(typed("/open claude"))

    assert "already open" in api.sent[-1]["text"]


async def test_something_running_with_no_window_is_still_opened(
    monkeypatch, on_a_mac, wired
) -> None:
    """The bug this replaced. An editor whose last window is closed keeps its
    process, its helpers and its language server alive — so `is running` says
    true and there is still nothing on screen. Reading that as "already open"
    reported an application that was nowhere to be seen."""
    channel, api = wired
    opened = standing(monkeypatch, running=True, on_screen=False)

    await channel._handle_message(typed("/open gemini"))

    assert opened == ["com.google.antigravity"]
    assert "running with no window" in api.sent[-1]["text"]


async def test_open_says_when_it_is_not_installed(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(None, False, False))

    await channel._handle_message(typed("/open codex"))

    assert "not installed" in api.sent[-1]["text"]


async def test_open_with_no_name_asks_which_one(monkeypatch, on_a_mac, wired) -> None:
    """Telegram's command menu pastes `/open` and stops, so the useful next
    thing is the question rather than a list to type from."""
    channel, api = wired
    closed(monkeypatch)

    await channel._handle_message(typed("/open"))

    assert "Open which one?" in api.sent[-1]["text"]
    offered = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert offered == {"claude", "codex", "antigravity"}


async def test_only_what_can_be_opened_is_offered(monkeypatch, on_a_mac, wired) -> None:
    """A button answering "it is already open" is a button that wasted a tap."""
    channel, api = wired
    monkeypatch.setattr(
        desktop,
        "status",
        lambda app: desktop.Status(Path("/x.app"), True, app.name == "claude"),
    )

    await channel._handle_message(typed("/open"))

    offered = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert offered == {"codex", "antigravity"}


async def test_nothing_to_offer_is_said_in_words(monkeypatch, on_a_mac, wired) -> None:
    """An empty keyboard would read as the question having failed."""
    channel, api = wired
    standing(monkeypatch, running=True, on_screen=True)

    await channel._handle_message(typed("/open"))

    assert "already open" in api.sent[-1]["text"]
    assert api.sent[-1].get("reply_markup") is None


async def test_nothing_installed_says_that_instead(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(None, False, False))

    await channel._handle_message(typed("/open"))

    assert "Nothing openable is installed" in api.sent[-1]["text"]


async def test_pressing_a_button_opens_that_one(monkeypatch, on_a_mac, wired) -> None:
    channel, _ = wired
    opened = closed(monkeypatch)

    await channel._handle_callback(
        {
            "id": "cb1",
            "from": {"id": int(APPROVER)},
            "data": cards.choice_data("open", "codex"),
            "message": {"message_id": 5, "chat": {"id": CHAT}},
        }
    )

    assert opened == ["com.openai.codex"]


async def test_somebody_else_pressing_it_opens_nothing(monkeypatch, on_a_mac, wired) -> None:
    channel, _ = wired

    def explode(app):
        raise AssertionError("an unauthorized press must open nothing")

    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(Path("/x.app"), False, False))
    monkeypatch.setattr(desktop, "open_", explode)

    await channel._handle_callback(
        {
            "id": "cb1",
            "from": {"id": 9999},
            "data": cards.choice_data("open", "codex"),
            "message": {"message_id": 5, "chat": {"id": CHAT}},
        }
    )


async def test_open_with_a_name_nobody_knows_says_so(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    closed(monkeypatch)

    await channel._handle_message(typed("/open opencode"))

    assert "do not know an application" in api.sent[-2]["text"]
    # Then asked, because a name nobody knows is usually one typed from memory.
    assert "Open which one?" in api.sent[-1]["text"]


async def test_open_off_a_mac_says_so_rather_than_pretending(monkeypatch, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "available", lambda: False)

    await channel._handle_message(typed("/open claude"))

    assert "macOS only" in api.sent[-1]["text"]


async def test_an_application_with_no_window_is_still_worth_offering(
    monkeypatch, on_a_mac, wired
) -> None:
    """Running is not the same as usable, which is the whole point here."""
    channel, api = wired
    standing(monkeypatch, running=True, on_screen=False)

    await channel._handle_message(typed("/open"))

    assert "Open which one?" in api.sent[-1]["text"]
