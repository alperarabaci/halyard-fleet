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
        self.sent.append({"chat_id": chat_id, "text": text})
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
    opened: list[str] = []
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(Path("/x.app"), False))
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


async def test_open_does_not_relaunch_something_already_running(
    monkeypatch, on_a_mac, wired
) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(Path("/x.app"), True))

    def explode(app):
        raise AssertionError("should not open something already open")

    monkeypatch.setattr(desktop, "open_", explode)
    await channel._handle_message(typed("/open claude"))

    assert "already open" in api.sent[-1]["text"]


async def test_open_says_when_it_is_not_installed(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "status", lambda app: desktop.Status(None, False))

    await channel._handle_message(typed("/open codex"))

    assert "not installed" in api.sent[-1]["text"]


async def test_open_with_no_name_lists_what_there_is(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    closed(monkeypatch)

    await channel._handle_message(typed("/open"))

    said = api.sent[-1]["text"]
    assert "claude" in said and "codex" in said and "antigravity" in said
    assert "gemini" in said  # the alias, so somebody learns what to type


async def test_open_with_a_name_nobody_knows_says_so(monkeypatch, on_a_mac, wired) -> None:
    channel, api = wired
    closed(monkeypatch)

    await channel._handle_message(typed("/open opencode"))

    assert "do not know an application" in api.sent[-1]["text"]


async def test_open_off_a_mac_says_so_rather_than_pretending(monkeypatch, wired) -> None:
    channel, api = wired
    monkeypatch.setattr(desktop, "available", lambda: False)

    await channel._handle_message(typed("/open claude"))

    assert "macOS only" in api.sent[-1]["text"]
