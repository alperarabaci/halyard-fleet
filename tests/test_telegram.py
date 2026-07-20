"""Tests for the Telegram channel.

Uses a fake Bot API. What matters here is not that HTTP works, but that the only
judgement this adapter makes — who is allowed to press the button — is made
correctly, and that everything it refuses is written down.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from halyard.channels.telegram import cards
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.core.approvals import ApprovalStore, Decision, ResolutionReason
from halyard.core.audit import AuditAction, AuditLog, JsonlAuditSink
from halyard.core.events import RiskLevel, Role

CHAT = "-1001234567890"
APPROVER = "4242"
STRANGER = "9999"
NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


class FakeTelegramApi:
    """Records what would have been sent, and answers plausibly."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []
        self.documents: list[dict] = []
        self.updates: list[list[dict]] = []
        self.opened = False
        self._next_message_id = 100

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def send_message(self, chat_id, text, *, reply_markup=None, **kwargs) -> dict:
        self._next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next_message_id}

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None, **kwargs):
        self.edits.append({"message_id": message_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id, *, text=None):
        self.answers.append({"id": callback_query_id, "text": text})

    async def send_document(self, chat_id, filename, content, *, caption=None) -> dict:
        self._next_message_id += 1
        self.documents.append({"filename": filename, "content": content, "caption": caption})
        return {"message_id": self._next_message_id}

    async def get_updates(self, *, offset=None, timeout=30) -> list[dict]:
        if self.updates:
            return self.updates.pop(0)
        await asyncio.sleep(0.01)
        return []


@pytest.fixture
async def setup(tmp_path: Path):
    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
    )
    try:
        yield channel, api, store, sink
    finally:
        await audit.close()


async def an_approval(store: ApprovalStore, **overrides):
    defaults = {
        "session_id": "a84ff5b4-289e-46af-be80-b88bb10a4349",
        "agent_id": "claude-code",
        "project": "alpha-engine",
        "tool": "Bash",
        "command_summary": "docker compose down postgres",
        "command_full": "docker compose down postgres",
        "risk": RiskLevel.HIGH,
        "role": Role.DRIVER,
    }
    return await store.create(**{**defaults, **overrides})


def press(request, action: str, *, user: str = APPROVER, nonce: str | None = None) -> dict:
    return {
        "id": "cbq-1",
        "from": {"id": int(user)},
        "data": f"hf:{cards.handle_of(request)}:{nonce or request.nonce}:{action}",
    }


# --- the 64-byte limit ------------------------------------------------------


async def test_callback_data_fits_telegrams_limit(setup) -> None:
    _, _, store, _ = setup
    request = await an_approval(store)

    for action in (cards.ALLOW, cards.DENY, cards.SHOW_FULL):
        data = cards.callback_data(request, action)
        assert len(data.encode("utf-8")) <= cards.CALLBACK_DATA_LIMIT


async def test_callback_data_round_trips(setup) -> None:
    _, _, store, _ = setup
    request = await an_approval(store)

    parsed = cards.parse_callback_data(cards.callback_data(request, cards.ALLOW))

    assert parsed == (cards.handle_of(request), request.nonce, cards.ALLOW)


@pytest.mark.parametrize(
    "data",
    ["", "nonsense", "hf:only:three", "other:a:b:c", "hf:a:b:x", "hf::b:a", "hf:a::a"],
)
def test_callback_data_that_is_not_ours_is_ignored(data: str) -> None:
    assert cards.parse_callback_data(data) is None


def test_an_oversized_handle_is_caught_when_the_card_is_built() -> None:
    from halyard.core.approvals import ApprovalRequest

    request = ApprovalRequest(
        request_id="req_" + "f" * 32,
        nonce="n" * 200,  # absurd, but the check must be the thing that notices
        session_id="s",
        agent_id="claude-code",
        project="p",
        tool="Bash",
        command_summary="ls",
        command_full="ls",
        risk=RiskLevel.LOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    # Better here than at send time: a card Telegram rejects is an approval
    # nobody ever sees, on the one path where that is the failure.
    with pytest.raises(ValueError, match="64-byte limit"):
        cards.callback_data(request, cards.ALLOW)


# --- the card ---------------------------------------------------------------


async def test_the_card_shows_what_is_needed_to_decide(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)

    await channel.send_approval_request(request)

    text = api.sent[0]["text"]
    assert "DRIVER — PERMISSION REQUEST" in text
    assert "🛑 HIGH" in text
    assert "alpha-engine" in text
    assert "docker compose down postgres" in text
    assert "Expires in" in text


async def test_allow_and_deny_sit_apart_from_anything_harmless(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store, command_full="x" * 500, command_summary="x" * 40)

    await channel.send_approval_request(request)

    rows = api.sent[0]["reply_markup"]["inline_keyboard"]
    assert [button["text"] for button in rows[0]] == ["Allow once", "Deny"]
    # A mistimed tap on "show me the rest" must not be able to land on "allow".
    assert rows[1][0]["text"] == "Show full command"


async def test_there_is_nothing_more_to_show_when_the_command_fits(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)

    await channel.send_approval_request(request)

    assert len(api.sent[0]["reply_markup"]["inline_keyboard"]) == 1


# --- deciding ---------------------------------------------------------------


async def test_the_approver_can_allow(setup) -> None:
    channel, _, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW))

    resolution = await store.resolution_of(request.request_id)
    assert resolution is not None
    assert resolution.decision is Decision.ALLOW
    assert resolution.decided_by == f"tg:{APPROVER}"
    assert resolution.reason is ResolutionReason.USER


async def test_the_approver_can_deny(setup) -> None:
    channel, _, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.DENY))

    resolution = await store.resolution_of(request.request_id)
    assert resolution is not None
    assert resolution.decision is Decision.DENY


async def test_the_card_is_rewritten_and_the_buttons_go_away(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW))

    # Scrolling back through a chat should show what was decided, not a row of
    # live-looking buttons on questions settled hours ago.
    assert api.edits[0]["reply_markup"] is None
    assert "✅ ALLOWED" in api.edits[0]["text"]
    assert f"tg:{APPROVER}" in api.edits[0]["text"]


async def test_the_full_command_can_be_shown_without_deciding_anything(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store, command_full="echo " + "x" * 300, command_summary="echo x…")
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.SHOW_FULL))

    assert await store.resolution_of(request.request_id) is None
    assert len(api.sent) == 2


async def test_a_command_too_long_for_a_message_is_sent_as_a_file(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store, command_full="x" * 5000, command_summary="x…")
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.SHOW_FULL))

    assert api.documents[0]["content"] == b"x" * 5000


# --- who is allowed to press ------------------------------------------------


async def test_a_stranger_cannot_decide_anything(setup) -> None:
    channel, _, store, sink = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW, user=STRANGER))

    assert await store.resolution_of(request.request_id) is None
    assert [r.action for r in await sink.read_all()] == [AuditAction.UNAUTHORIZED_CALLBACK]


async def test_a_stranger_is_told_nothing(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW, user=STRANGER))

    # The spinner is dismissed so the button does not look broken, but nothing
    # comes back that would confirm the request exists or that this bot is
    # involved with it.
    assert api.answers == [{"id": "cbq-1", "text": None}]
    assert api.edits == []


async def test_the_stranger_is_named_in_the_audit_log(setup) -> None:
    channel, _, store, sink = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW, user=STRANGER))

    record = (await sink.read_all())[0]
    assert record.actor == f"tg:{STRANGER}"
    assert record.request_id == request.request_id


# --- replay and forgery -----------------------------------------------------


async def test_pressing_the_same_button_twice_changes_nothing(setup) -> None:
    channel, _, store, sink = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)
    await channel._handle_callback(press(request, cards.ALLOW))

    await channel._handle_callback(press(request, cards.DENY))

    resolution = await store.resolution_of(request.request_id)
    assert resolution is not None
    assert resolution.decision is Decision.ALLOW
    assert AuditAction.REPLAYED_CALLBACK in {r.action for r in await sink.read_all()}


async def test_a_forged_nonce_is_refused_and_recorded(setup) -> None:
    channel, api, store, sink = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW, nonce="not-the-real-nonce"))

    assert await store.resolution_of(request.request_id) is None
    assert AuditAction.INVALID_NONCE in {r.action for r in await sink.read_all()}
    # Nothing said back. A forged press learns whether it worked only by whether
    # the command ran, which it did not.
    assert api.answers == [{"id": "cbq-1", "text": None}]


async def test_a_press_after_the_deadline_denies(tmp_path: Path) -> None:
    store = ApprovalStore(ttl=timedelta(milliseconds=1))
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
    )
    request = await an_approval(store)
    await channel.send_approval_request(request)
    await asyncio.sleep(0.02)

    await channel._handle_callback(press(request, cards.ALLOW))

    resolution = await store.resolution_of(request.request_id)
    assert resolution is not None
    assert resolution.decision is Decision.DENY
    assert "Too late" in api.answers[0]["text"]
    await audit.close()


async def test_a_press_on_a_request_this_process_never_saw_is_refused(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)
    # Never sent, so the adapter has no handle for it — a restarted control
    # plane, or a card from a previous run.
    await channel._handle_callback(press(request, cards.ALLOW))

    assert await store.resolution_of(request.request_id) is None
    assert api.answers[0]["text"] == "That request is no longer open."


# --- the polling loop -------------------------------------------------------


async def test_the_loop_keeps_going_after_a_telegram_outage(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    failed = {"once": False}
    original = api.get_updates

    async def flaky(**kwargs):
        if not failed["once"]:
            failed["once"] = True
            raise ConnectionError("telegram is down")
        return await original(**kwargs)

    api.get_updates = flaky
    api.updates = [[{"update_id": 1, "callback_query": press(request, cards.ALLOW)}]]

    await channel.start()
    for _ in range(200):
        if await store.resolution_of(request.request_id):
            break
        await asyncio.sleep(0.02)
    await channel.stop()

    # If this loop stayed down, every approval would sit until its deadline and
    # be denied. Safe, but silent.
    assert (await store.resolution_of(request.request_id)) is not None


async def test_one_bad_update_does_not_silence_the_rest(setup) -> None:
    channel, api, store, _ = setup
    request = await an_approval(store)
    await channel.send_approval_request(request)

    api.updates = [
        [
            {"update_id": 1, "callback_query": {"id": "x"}},  # no data, no from
            {"update_id": 2, "callback_query": press(request, cards.ALLOW)},
        ]
    ]

    await channel.start()
    for _ in range(200):
        if await store.resolution_of(request.request_id):
            break
        await asyncio.sleep(0.02)
    await channel.stop()

    assert (await store.resolution_of(request.request_id)) is not None


async def test_stopping_closes_the_connection(setup) -> None:
    channel, api, _, _ = setup

    await channel.start()
    assert api.opened
    await channel.stop()

    assert not api.opened


# --- the token ---------------------------------------------------------------


def test_the_api_object_never_prints_its_token() -> None:
    from halyard.channels.telegram.api import TelegramApi

    api = TelegramApi("123456:SUPER-SECRET-BOT-TOKEN")

    # A traceback that happens to include this object must not paste a bot token
    # into a log or an audit record.
    assert "SUPER-SECRET" not in repr(api)
    assert "SUPER-SECRET" not in str(api)
