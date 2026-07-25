"""Tests for the Telegram channel.

Uses a fake Bot API. What matters here is not that HTTP works, but that the only
judgement this adapter makes — who is allowed to press the button — is made
correctly, and that everything it refuses is written down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from halyard.channels.telegram import adapter, cards
from halyard.channels.telegram.adapter import TelegramChannel, _SessionTarget
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
        self.commands: tuple[tuple[str, str], ...] = ()
        self.opened = False
        self._next_message_id = 100

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def set_my_commands(self, commands) -> None:
        self.commands = tuple(commands)

    async def send_message(
        self,
        chat_id,
        text,
        *,
        reply_markup=None,
        message_thread_id=None,
        reply_to_message_id=None,
        **kwargs,
    ) -> dict:
        self._next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return {"message_id": self._next_message_id}

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None, **kwargs):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id, *, text=None):
        self.answers.append({"id": callback_query_id, "text": text})

    async def send_document(
        self, chat_id, filename, content, *, caption=None, message_thread_id=None
    ) -> dict:
        self._next_message_id += 1
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "content": content,
                "caption": caption,
                "message_thread_id": message_thread_id,
            }
        )
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
        poll_retry_seconds=0.01,
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


async def test_a_long_outage_logs_one_traceback_and_then_counts(setup, caplog) -> None:
    channel, api, _, _ = setup

    async def always_fails(**kwargs):
        raise ConnectionError("[Errno -3] Temporary failure in name resolution")

    api.get_updates = always_fails

    with caplog.at_level(logging.ERROR, logger="halyard.channels.telegram.adapter"):
        await channel.start()
        await asyncio.sleep(0.2)
        await channel.stop()

    records = [r for r in caplog.records if "poll" in r.message.lower()]
    # A transient DNS failure that printed eighteen identical tracebacks is what
    # prompted this. One traceback tells you what broke; the rest is noise that
    # makes a recoverable blip look like a crash.
    assert sum(1 for r in records if r.exc_info) == 1
    assert len(records) > 1
    assert any("consecutive" in r.message for r in records)


async def test_recovery_is_announced(setup, caplog) -> None:
    channel, api, _, _ = setup
    failed = {"once": False}
    original = api.get_updates

    async def flaky(**kwargs):
        if not failed["once"]:
            failed["once"] = True
            raise ConnectionError("telegram is down")
        return await original(**kwargs)

    api.get_updates = flaky

    with caplog.at_level(logging.WARNING, logger="halyard.channels.telegram.adapter"):
        await channel.start()
        await asyncio.sleep(0.2)
        await channel.stop()

    # Errors stopping is not something anyone notices in a log. A line saying
    # they stopped is.
    assert any("recovered" in r.message for r in caplog.records)


def test_backoff_grows_and_then_stops_growing() -> None:
    channel = TelegramChannel(
        api=FakeTelegramApi(),
        store=ApprovalStore(),
        audit=AuditLog([]),
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        poll_retry_seconds=3.0,
    )

    assert [channel._backoff(n) for n in (1, 2, 3, 4)] == [3.0, 6.0, 12.0, 24.0]
    # Capped, so a long outage does not turn into a long silence after it ends.
    assert channel._backoff(20) == 30.0


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


# --- typed commands -----------------------------------------------------------


def typed(text: str, *, user: str = APPROVER) -> dict:
    return {"message_id": 1, "from": {"id": int(user)}, "text": text}


async def gated(tmp_path: Path):
    from halyard.core.gate import Gate

    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    api = FakeTelegramApi()
    gate = Gate()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        poll_retry_seconds=0.01,
        gate=gate,
        project="alpha-engine",
    )
    return channel, api, gate, sink


async def test_pause_closes_the_gate_and_says_so(tmp_path: Path) -> None:
    channel, api, gate, sink = await gated(tmp_path)

    await channel._handle_message(typed("/pause"))

    assert gate.paused is True
    # It has to say the replies stop too, so the silence afterwards does not
    # read as something being broken.
    assert "no approval cards, no replies" in api.sent[0]["text"]
    # And it has to say who decides instead. This assertion used to require the
    # words "nothing is being auto-approved", which was measured to be false:
    # pausing hands the decision to Claude Code, whose own permissions.allow
    # list then runs matching commands with no prompt at all. Saying otherwise
    # made pausing sound stricter than it is, in the one message somebody reads
    # while deciding whether it is safe to walk away.
    assert "permissions.allow" in api.sent[0]["text"]
    assert AuditAction.GATE_PAUSED in {r.action for r in await sink.read_all()}


async def test_pause_twice_confirms_rather_than_complains(tmp_path: Path) -> None:
    channel, api, gate, sink = await gated(tmp_path)
    await channel._handle_message(typed("/pause"))

    await channel._handle_message(typed("/pause"))

    assert gate.paused is True
    assert "Already paused" in api.sent[1]["text"]
    # Idempotent all the way down: no second record for a change that did not
    # happen.
    assert len([r for r in await sink.read_all() if r.action is AuditAction.GATE_PAUSED]) == 1


async def test_resume_reopens_the_gate(tmp_path: Path) -> None:
    channel, _api, gate, sink = await gated(tmp_path)
    await channel._handle_message(typed("/pause"))

    await channel._handle_message(typed("/resume"))

    assert gate.paused is False
    assert AuditAction.GATE_RESUMED in {r.action for r in await sink.read_all()}


async def test_resume_twice_confirms(tmp_path: Path) -> None:
    channel, api, gate, _ = await gated(tmp_path)

    await channel._handle_message(typed("/resume"))

    assert gate.paused is False
    assert "Already running" in api.sent[0]["text"]


async def test_status_reports_the_gate_and_what_is_open(tmp_path: Path) -> None:
    channel, api, _, _ = await gated(tmp_path)
    await channel._handle_message(typed("/pause"))

    await channel._handle_message(typed("/status"))

    text = api.sent[-1]["text"]
    assert "paused" in text
    assert "Open approvals: 0" in text


async def test_a_stranger_cannot_close_the_gate(tmp_path: Path) -> None:
    channel, api, gate, sink = await gated(tmp_path)

    await channel._handle_message(typed("/pause", user=STRANGER))

    # Closing the gate stops anyone being asked. A stranger must not be able to
    # do that any more than they can approve something.
    assert gate.paused is False
    assert api.sent == []
    assert AuditAction.UNAUTHORIZED_CALLBACK in {r.action for r in await sink.read_all()}


@pytest.mark.parametrize("text", ["/pause@halyard_bot", "/PAUSE", "/pause now"])
async def test_a_command_is_recognised_however_it_is_typed(tmp_path: Path, text: str) -> None:
    channel, _, gate, _ = await gated(tmp_path)

    await channel._handle_message(typed(text))

    assert gate.paused is True


# --- keeping a navigator and a driver apart -----------------------------------

NAV_CHAT = "-1001111111111"
DRV_CHAT = "-1002222222222"


async def routed(tmp_path: Path, *, navigator=NAV_CHAT, driver=DRV_CHAT, ttl=timedelta(minutes=5)):
    store = ApprovalStore(ttl=ttl)
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
        poll_retry_seconds=0.01,
        navigator_chat_id=navigator,
        driver_chat_id=driver,
    )
    return channel, api, store


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.NAVIGATOR, NAV_CHAT), (Role.DRIVER, DRV_CHAT), (None, CHAT)],
)
async def test_a_card_goes_to_its_own_seat(tmp_path: Path, role, expected: str) -> None:
    channel, api, store = await routed(tmp_path)
    request = await an_approval(store, role=role)

    await channel.send_approval_request(request)

    assert api.sent[0]["chat_id"] == expected


async def test_a_topic_can_stand_in_for_a_group(tmp_path: Path) -> None:
    channel, api, store = await routed(tmp_path, navigator=f"{NAV_CHAT}:12")
    request = await an_approval(store, role=Role.NAVIGATOR)

    await channel.send_approval_request(request)

    # One syntax covers both shapes a group can take — a chat of its own, or a
    # forum topic inside a shared one.
    assert api.sent[0]["chat_id"] == NAV_CHAT
    assert api.sent[0]["message_thread_id"] == 12


async def test_replies_follow_the_same_route(tmp_path: Path) -> None:
    channel, api, _ = await routed(tmp_path)

    await channel.send_message("session-1", "done", Role.DRIVER)

    assert api.sent[0]["chat_id"] == DRV_CHAT


async def test_replies_choose_runtime_specific_seat_when_roles_match(tmp_path: Path) -> None:
    from halyard.core.seats import Seat

    channel, api, _ = await routed(tmp_path)
    channel._seats = [
        Seat("drv", "claude-code", "claude-session", DRV_CHAT, Role.DRIVER),
        Seat("xdrv", "codex", "codex-session", "-1003333333333", Role.DRIVER),
    ]

    await channel.send_message(
        "codex-session",
        "done",
        Role.DRIVER,
        agent_id="codex",
        session_name="alpha-engine-xdriver",
    )

    assert api.sent[0]["chat_id"] == "-1003333333333"


async def test_a_long_reply_follows_the_route_too(tmp_path: Path) -> None:
    channel, api, _ = await routed(tmp_path)

    await channel.send_long_content("session-1", "x" * 5000, "Agent reply", Role.NAVIGATOR)

    assert api.documents[0]["chat_id"] == NAV_CHAT


async def test_a_long_reply_stays_in_its_forum_topic(tmp_path: Path) -> None:
    channel, api, _ = await routed(tmp_path, navigator=f"{NAV_CHAT}:12")

    await channel.send_long_content("session-1", "x" * 5000, "Agent reply", Role.NAVIGATOR)

    assert api.documents[0]["chat_id"] == NAV_CHAT
    assert api.documents[0]["message_thread_id"] == 12


# --- an existing single-chat setup must not notice any of this ----------------


@pytest.mark.parametrize("role", [Role.NAVIGATOR, Role.DRIVER, None])
async def test_without_seats_everything_lands_where_it_always_did(tmp_path: Path, role) -> None:
    channel, api, store = await routed(tmp_path, navigator=None, driver=None)
    request = await an_approval(store, role=role)

    await channel.send_approval_request(request)
    await channel.send_message("session-1", "done", role)

    # Nobody who has not opted in should see a behaviour change.
    assert {message["chat_id"] for message in api.sent} == {CHAT}
    assert all(message["message_thread_id"] is None for message in api.sent)


async def test_a_seat_that_is_configured_alone_still_leaves_the_other_default(
    tmp_path: Path,
) -> None:
    channel, api, store = await routed(tmp_path, driver=None)

    await channel.send_approval_request(await an_approval(store, role=Role.NAVIGATOR))
    await channel.send_approval_request(await an_approval(store, role=Role.DRIVER))

    assert [message["chat_id"] for message in api.sent] == [NAV_CHAT, CHAT]


async def test_a_named_session_reaches_its_seat_without_any_shell(tmp_path: Path) -> None:
    """The desktop-app path, end to end: no shell, no HALYARD_ROLE, just a name."""
    from halyard.core.policy import Policy
    from halyard.core.redaction import Redactor
    from halyard.core.registry import SessionRegistry
    from halyard.core.service import ApprovalService

    # A short deadline: nobody answers this card, and the test should not
    # sit through the real one.
    channel, api, store = await routed(tmp_path, ttl=timedelta(milliseconds=50))
    sink = JsonlAuditSink(tmp_path / "service.jsonl")
    await sink.open()
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=channel,
        project="alpha-engine",
        seats={"alpha-engine-navigator": Role.NAVIGATOR},
    )

    await service.request(
        session_id="s",
        agent_id="claude-code",
        tool="Bash",
        command="ls",
        session_name="alpha-engine-navigator",
    )

    # Nothing set HALYARD_ROLE anywhere. The name did the routing.
    assert api.sent[0]["chat_id"] == NAV_CHAT
    assert "NAVIGATOR" in api.sent[0]["text"]


async def test_a_routed_card_is_settled_in_the_chat_it_was_sent_to(tmp_path: Path) -> None:
    channel, api, store = await routed(tmp_path)
    request = await an_approval(store, role=Role.DRIVER)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.ALLOW))

    # Routing the send but not the edit leaves the buttons live on a settled
    # card: the edit goes to the default chat with a message id from another
    # one, fails, and is only logged. Pressing Allow appeared to do nothing.
    assert api.sent[0]["chat_id"] == DRV_CHAT
    assert api.edits[0]["chat_id"] == DRV_CHAT
    assert api.edits[0]["reply_markup"] is None
    assert "✅ ALLOWED" in api.edits[0]["text"]


async def test_an_unrouted_card_is_still_settled_in_the_default_chat(tmp_path: Path) -> None:
    channel, api, store = await routed(tmp_path, navigator=None, driver=None)
    request = await an_approval(store, role=Role.DRIVER)
    await channel.send_approval_request(request)

    await channel._handle_callback(press(request, cards.DENY))

    assert api.edits[0]["chat_id"] == CHAT


# --- typing into a session ----------------------------------------------------


class FakeRunner:
    """Records what would have been sent into a session."""

    id = "claude-code"
    available = True

    def __init__(self, *, works: bool = True, default_model: str | None = None) -> None:
        self.default_model = default_model
        # The channel asks its seat's runtime what a name means, rather than
        # importing one runtime's lookup. A double has to answer that too.
        self.sessions: dict[str, object] = {}
        self.sent: list[tuple[str, str]] = []
        self.directories: list[str | None] = []
        self.working: set[str] = set()
        self.models: dict[str, str] = {}
        self.efforts: dict[str, str] = {}
        self._works = works

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        # The same sets the real runner reports. A double that accepts less
        # than the thing it stands in for fails tests the real code passes.
        return {
            "model": (("opus", "sonnet", "haiku", "fable"), False),
            "effort": (("low", "medium", "high", "xhigh", "max"), True),
        }

    def resolve(self, name: str):
        return self.sessions.get(name)

    def busy(self, session_id: str) -> bool:
        return session_id in self.working

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        return self.models.get(session_id) or self.default_model, self.efforts.get(session_id)

    def set_model(self, session_id: str, model: str | None) -> None:
        if model:
            self.models[session_id] = model
        else:
            self.models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
        if effort:
            self.efforts[session_id] = effort
        else:
            self.efforts.pop(session_id, None)

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        self.sent.append((session_id, text))
        self.directories.append(cwd)
        return self._works


async def wired(tmp_path: Path, *, works: bool = True, seat=None, chat=None):
    """A channel that can actually deliver a message into a session."""
    from halyard.core.gate import Gate
    from halyard.core.registry import SessionRegistry

    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    registry = SessionRegistry()
    await registry.observe(
        session_id="session-nav",
        agent_id="claude-code",
        project="alpha-engine",
        role=Role.NAVIGATOR,
    )
    await registry.observe(
        session_id="session-drv",
        agent_id="claude-code",
        project="alpha-engine",
        role=Role.DRIVER,
    )
    api = FakeTelegramApi()
    runner = FakeRunner(works=works)
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        poll_retry_seconds=0.01,
        gate=Gate(),
        project="alpha-engine",
        navigator_chat_id=NAV_CHAT,
        driver_chat_id=DRV_CHAT,
        registry=registry,
        runner=runner,
    )
    return channel, api, runner, sink


def typed_in(text: str, chat: str, *, user: str = APPROVER) -> dict:
    return {"message_id": 1, "from": {"id": int(user)}, "chat": {"id": chat}, "text": text}


async def drain() -> None:
    """Let the detached delivery task finish."""
    await asyncio.sleep(0.05)


async def test_typing_in_a_seat_reaches_that_seat_s_session(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("run the tests", NAV_CHAT))
    await drain()

    # The message lands in the session itself, not a side conversation, so it
    # is in the history when that session is opened at a desk later.
    assert runner.sent == [("session-nav", "run the tests")]


async def test_each_seat_reaches_its_own_session(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("navigator work", NAV_CHAT))
    await channel._handle_message(typed_in("driver work", DRV_CHAT))
    await drain()

    assert runner.sent == [("session-nav", "navigator work"), ("session-drv", "driver work")]


async def multi_runtime_wired(tmp_path: Path):
    """A Claude default plus a Codex seat, matching the production shape."""
    from halyard.agents.base import SessionRef
    from halyard.core.gate import Gate
    from halyard.core.registry import SessionRegistry
    from halyard.core.seats import Seat

    sink = JsonlAuditSink(tmp_path / "multi-runtime-audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    api = FakeTelegramApi()
    claude = FakeRunner()
    codex = FakeRunner(default_model="gpt-5.6-codex")
    codex.id = "codex"
    codex.sessions["alpha-engine-xdriver"] = SessionRef(
        "codex-session", "alpha-engine-xdriver", "/repo", "gpt-5.6-codex", "high"
    )
    chat = "-100444"
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        gate=Gate(),
        project="alpha-engine",
        registry=SessionRegistry(),
        runner=claude,
        runners={"claude-code": claude, "codex": codex},
        seats=[
            Seat(
                label="xdrv",
                runtime="codex",
                session="alpha-engine-xdriver",
                chat=chat,
                role=Role.DRIVER,
            )
        ],
    )
    return channel, api, claude, codex, chat


async def test_a_codex_seat_delivers_with_the_codex_runner(tmp_path: Path) -> None:
    channel, _, claude, codex, chat = await multi_runtime_wired(tmp_path)

    await channel._handle_message(typed_in("continue from Telegram", chat))
    await drain()

    assert claude.sent == []
    assert codex.sent == [("codex-session", "continue from Telegram")]


async def test_a_codex_seat_uses_codex_for_options_and_preferences(tmp_path: Path) -> None:
    channel, api, claude, codex, chat = await multi_runtime_wired(tmp_path)
    codex.options = lambda session_id=None: {
        "model": (("gpt-5.6-codex",), False),
        "effort": (("low", "high", "ultra"), True),
    }

    await channel._handle_message(typed_in("/options", chat))
    assert "codex" in api.sent[-1]["text"]
    assert "ultra" in api.sent[-1]["text"]

    await channel._handle_message(typed_in("/effort ultra", chat))
    assert codex.efforts["codex-session"] == "ultra"
    assert claude.efforts == {}


async def test_delivery_does_not_block_the_caller(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(session_id: str, text: str, cwd: str | None = None) -> bool:
        started.set()
        await release.wait()
        return True

    runner.send = slow

    await channel._handle_message(typed_in("something long-running", NAV_CHAT))

    # A turn runs tools, and each one may stop for an approval that arrives as
    # a button press this same loop has to read. Waiting here for the turn would
    # be waiting for an approval that can never be delivered.
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()


async def test_a_stranger_cannot_type_into_a_session(tmp_path: Path) -> None:
    channel, _, runner, sink = await wired(tmp_path)

    await channel._handle_message(typed_in("do something", NAV_CHAT, user=STRANGER))
    await drain()

    assert runner.sent == []
    assert AuditAction.UNAUTHORIZED_CALLBACK in {r.action for r in await sink.read_all()}


async def test_a_message_is_recorded_without_its_text(tmp_path: Path) -> None:
    channel, _, _, sink = await wired(tmp_path)

    await channel._handle_message(typed_in("a distinctive instruction", NAV_CHAT))
    await drain()

    record = next(r for r in await sink.read_all() if r.action is AuditAction.USER_MESSAGE)
    assert record.session_id == "session-nav"
    assert record.actor == f"tg:{APPROVER}"
    assert record.detail["delivered"] is True
    # Same reasoning as an agent's reply: the chat holds what was said.
    assert "distinctive instruction" not in (tmp_path / "audit.jsonl").read_text()


async def test_a_failed_delivery_says_so(tmp_path: Path) -> None:
    channel, api, _, sink = await wired(tmp_path, works=False)

    await channel._handle_message(typed_in("run the tests", NAV_CHAT))
    await drain()

    assert "did not reach" in api.sent[-1]["text"]
    record = next(r for r in await sink.read_all() if r.action is AuditAction.USER_MESSAGE)
    assert record.detail["delivered"] is False


async def test_a_paused_gate_refuses_to_forward(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)
    await channel._handle_message(typed("/pause"))
    api.sent.clear()

    await channel._handle_message(typed_in("do something", NAV_CHAT))
    await drain()

    # Pausing means the phone is disconnected in both directions, including
    # this one.
    assert runner.sent == []
    assert "Paused" in api.sent[-1]["text"]


async def test_a_control_plane_without_a_runner_says_why(tmp_path: Path) -> None:
    channel, api, _, _ = await gated(tmp_path)

    await channel._handle_message(typed_in("do something", CHAT))
    await drain()

    # A container has no claude CLI. Better to say that than to fail silently.
    assert "host" in api.sent[-1]["text"]


async def test_commands_are_still_commands(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/status", NAV_CHAT))
    await drain()

    assert runner.sent == []
    assert "Halyard" in api.sent[-1]["text"]


async def test_a_configured_name_finds_the_session_without_any_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path that matters after a restart, when the registry knows nothing."""
    from halyard.core.gate import Gate
    from halyard.core.registry import SessionRegistry

    root = tmp_path / "projects" / "-repo"
    root.mkdir(parents=True)
    (root / "72704a07-2785-45df-980c-231f318d00c5.jsonl").write_text(
        '{"type":"custom-title","customTitle":"alpha-engine-navigator","sessionId":"x"}\n'
        '{"type":"assistant","cwd":"/repos/alpha-engine","sessionId":"x"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "halyard.agents.claude_code.sessions.transcript_root", lambda: tmp_path / "projects"
    )

    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    api = FakeTelegramApi()
    runner = FakeRunner()
    # The real lookup, because that is what this test is about: a name is
    # addressable from a standing start, out of a transcript on disk. The
    # channel asks its seat's runtime, so the double has to hand that question
    # to the runtime rather than answer it itself.
    from halyard.agents.claude_code import find_session as real_lookup

    runner.resolve = real_lookup
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        gate=Gate(),
        project="alpha-engine",
        navigator_chat_id=NAV_CHAT,
        # Deliberately empty: nothing has fired a hook since this came up.
        registry=SessionRegistry(),
        runner=runner,
        session_names={Role.NAVIGATOR: "alpha-engine-navigator"},
    )

    await channel._handle_message(typed_in("summarise where we are", NAV_CHAT))
    await drain()

    # Telling somebody to go run a command somewhere before they can send a
    # message is not an answer, so the name is addressable from a standing start.
    assert runner.sent == [("72704a07-2785-45df-980c-231f318d00c5", "summarise where we are")]
    # And run there, because `--resume` looks for a conversation inside the
    # current project: from anywhere else it reports no such session, even with
    # the transcript sitting on disk.
    assert runner.directories == ["/repos/alpha-engine"]


async def test_chat_says_the_same_thing_explicitly(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/chat run the tests", NAV_CHAT))
    await drain()

    # Needed by anyone who leaves group privacy mode on, where a bot sees only
    # commands.
    assert runner.sent == [("session-nav", "run the tests")]


async def test_chat_without_a_message_explains_itself(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/chat", NAV_CHAT))
    await drain()

    assert runner.sent == []
    assert "Usage" in api.sent[-1]["text"]


# --- knowing what is answering, and whether it is busy -------------------------


async def test_status_shows_what_each_seat_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard.agents.claude_code import SessionRef

    channel, api, _runner, _ = await wired(tmp_path)
    channel._session_names = {Role.NAVIGATOR: "nav", Role.DRIVER: "drv"}
    refs = {
        "nav": SessionRef("id-nav", "nav", "/repo", "claude-opus-4-8", "xhigh"),
        "drv": SessionRef("id-drv", "drv", "/repo", "claude-opus-4-8", "low"),
    }
    _runner.sessions = refs

    await channel._handle_message(typed("/status"))

    text = api.sent[-1]["text"]
    # Which model a seat is on is invisible from a phone otherwise, and in a
    # navigator/driver pair the two are deliberately different.
    assert "claude-opus-4-8" in text
    assert "effort xhigh" in text
    assert "effort low" in text


async def test_status_says_when_a_seat_is_mid_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard.agents.claude_code import SessionRef

    channel, api, runner, _ = await wired(tmp_path)
    channel._session_names = {Role.NAVIGATOR: "nav"}
    runner.sessions = {"nav": SessionRef("id-nav", "nav", "/repo", "claude-opus-4-8", "xhigh")}
    runner.working.add("id-nav")

    await channel._handle_message(typed("/status"))

    assert "working" in api.sent[-1]["text"]


async def test_a_message_sent_while_a_turn_is_running_says_it_is_queued(
    tmp_path: Path,
) -> None:
    channel, api, runner, _ = await wired(tmp_path)
    runner.working.add("session-nav")

    await channel._handle_message(typed_in("and another thing", NAV_CHAT))
    await drain()

    # Sends are serialised per session, so this would otherwise sit in silence
    # until the turn ahead of it finished — and silence is what makes people
    # think a message was lost.
    assert "queued" in api.sent[0]["text"]
    assert runner.sent == [("session-nav", "and another thing")]


async def test_a_missing_seat_is_reported_rather_than_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel, api, runner, _ = await wired(tmp_path)
    channel._session_names = {Role.NAVIGATOR: "a name that is not there"}
    runner.sessions = {}

    await channel._handle_message(typed("/status"))

    # A name copied slightly wrong routes nothing and explains nothing, so the
    # place you go to check has to say it could not find it.
    assert "not found" in api.sent[-1]["text"]


async def test_the_bot_answers_in_the_chat_that_asked(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/status", DRV_CHAT))

    # Ask in the driver group and the reply used to appear in a private chat
    # with the bot, which reads exactly like the message having been lost.
    assert api.sent[-1]["chat_id"] == DRV_CHAT


async def test_a_queued_notice_lands_where_the_message_was_typed(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)
    runner.working.add("session-nav")

    await channel._handle_message(typed_in("another thing", NAV_CHAT))
    await drain()

    assert api.sent[0]["chat_id"] == NAV_CHAT


async def test_a_reply_stays_in_its_forum_topic(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)
    message = typed_in("/status", NAV_CHAT)
    message["message_thread_id"] = 12

    await channel._handle_message(message)

    assert api.sent[-1]["message_thread_id"] == 12


# --- choosing what answers ----------------------------------------------------


async def test_model_can_be_set_from_the_chat(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/model claude-sonnet-5", NAV_CHAT))

    assert runner.models["session-nav"] == "claude-sonnet-5"
    assert "claude-sonnet-5" in api.sent[-1]["text"]


async def test_effort_can_be_set_from_the_chat(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/effort low", DRV_CHAT))

    assert runner.efforts["session-drv"] == "low"


async def test_each_seat_keeps_its_own_choice(tmp_path: Path) -> None:
    channel, _, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/effort xhigh", NAV_CHAT))
    await channel._handle_message(typed_in("/effort low", DRV_CHAT))

    # The point of a navigator and a driver is that they are not the same.
    assert runner.efforts["session-nav"] == "xhigh"
    assert runner.efforts["session-drv"] == "low"


@pytest.mark.parametrize("value", ["default", "clear", "reset"])
async def test_a_choice_can_be_given_back(tmp_path: Path, value: str) -> None:
    channel, _, runner, _ = await wired(tmp_path)
    await channel._handle_message(typed_in("/model claude-sonnet-5", NAV_CHAT))

    await channel._handle_message(typed_in(f"/model {value}", NAV_CHAT))

    assert runner.models.get("session-nav") is None


async def test_a_nonsense_effort_is_refused_before_it_costs_a_turn(tmp_path: Path) -> None:
    channel, api, runner, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/effort enormous", NAV_CHAT))

    # A closed set, so a typo is caught here rather than by a turn that fails a
    # minute later.
    assert runner.efforts.get("session-nav") is None
    assert "low medium high" in api.sent[-1]["text"]


# --- long replies stay readable ----------------------------------------------


async def test_a_long_reply_is_split_across_messages(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)
    reply = "\n".join(f"line {n} of a long answer" for n in range(600))

    await channel.send_message("session-nav", reply, Role.NAVIGATOR)

    # Not a .txt: on a phone that has to be tapped, downloaded and opened, and
    # reading the reply where it lands is the entire point.
    assert api.documents == []
    assert len(api.sent) > 1
    assert all(len(m["text"]) <= cards.MESSAGE_LIMIT for m in api.sent)
    assert "(1/" in api.sent[0]["text"]


async def test_a_short_reply_is_one_message_with_no_marker(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)

    await channel.send_message("session-nav", "done", Role.NAVIGATOR)

    assert len(api.sent) == 1
    assert api.sent[0]["text"] == "done"


async def test_a_reply_that_looks_like_markup_survives(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)

    await channel.send_message("session-nav", "wrap it in a <div> & move on", Role.NAVIGATOR)

    # Sent as HTML, an agent mentioning a tag makes Telegram refuse the whole
    # message — and the reply disappears with only a log line to show for it.
    assert "&lt;div&gt;" in api.sent[0]["text"]
    assert "&amp;" in api.sent[0]["text"]


def test_splitting_prefers_line_boundaries() -> None:
    text = "\n".join("x" * 100 for _ in range(100))

    chunks = cards.split_for_telegram(text, limit=1000)

    assert len(chunks) > 1
    # A code block cut mid-token is harder to read than one cut between lines.
    assert all(not c.startswith("x" * 100 + "x") for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_a_single_enormous_line_is_still_sent() -> None:
    chunks = cards.split_for_telegram("y" * 5000, limit=1000)

    # Cut anyway: the alternative is not sending it.
    assert len(chunks) == 5
    assert all(len(c) <= 1000 for c in chunks)


async def test_options_lists_what_the_runtime_accepts(tmp_path: Path) -> None:
    """One question, everything answerable.

    Asked from a phone, where the alternative is guessing and paying a round
    trip for each wrong guess.
    """
    channel, api, _, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/options", NAV_CHAT))

    text = api.sent[-1]["text"]
    assert "claude-code" in text
    for value in ("opus", "haiku", "low", "max"):
        assert value in text


async def test_options_says_which_values_are_only_a_hint(tmp_path: Path) -> None:
    """A model that shipped this morning is not on any list written before it.

    Printing the models as if they were the whole set would make a working
    name look unavailable, which is the failure this command exists to avoid.
    """
    channel, api, _, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/options", NAV_CHAT))

    text = api.sent[-1]["text"]
    assert "passed through" in text
    # Said about models, which are open, and not about effort, which is not.
    assert text.count("passed through") == 1


async def test_options_comes_from_the_runtime_not_from_this_module(tmp_path: Path) -> None:
    """A second runtime — Codex, whatever follows — needs no change here."""
    channel, api, runner, _ = await wired(tmp_path)
    runner.id = "codex"
    runner.options = lambda session_id=None: {"model": (("gpt-nitro",), True)}

    await channel._handle_message(typed_in("/options", NAV_CHAT))

    text = api.sent[-1]["text"]
    assert "codex" in text
    assert "gpt-nitro" in text
    assert "passed through" not in text


async def test_clearing_a_model_restores_session_inheritance(tmp_path: Path) -> None:
    channel, api, _, _ = await wired(tmp_path)

    await channel._handle_message(typed_in("/model opus", NAV_CHAT))
    await channel._handle_message(typed_in("/model default", NAV_CHAT))

    text = api.sent[-1]["text"]
    assert "resumed session/runtime" in text


async def test_status_says_when_phone_turns_inherit_the_session(
    tmp_path: Path, monkeypatch
) -> None:
    from halyard.agents.claude_code import SessionRef

    channel, api, _runner, _ = await wired(tmp_path)
    channel._session_names = {Role.NAVIGATOR: "nav"}
    _runner.sessions = {
        "nav": SessionRef("session-nav", "nav", "/repo", "claude-opus-4-8", "xhigh")
    }

    await channel._handle_message(typed_in("/status", NAV_CHAT))

    text = api.sent[-1]["text"]
    assert "claude-opus-4-8" in text
    assert "at the desk" in text
    assert "from here" in text
    assert "inherits the session/runtime" in text


async def test_effort_is_validated_against_the_runtime_not_a_hardcoded_list(tmp_path) -> None:
    """The channel must not carry one runtime's constants.

    It imported Claude Code's EFFORT_LEVELS and checked against that, so a
    runtime with a different set — Codex has an `ultra` on some models and no
    `max` on others — would have had valid input refused by the chat layer.
    """
    channel, api, runner, _ = await wired(tmp_path)
    runner.options = lambda session_id=None: {
        "effort": (("gentle", "fierce"), True),
        "model": ((), False),
    }

    await channel._handle_message(typed_in("/effort fierce", NAV_CHAT))
    assert runner.efforts["session-nav"] == "fierce"

    await channel._handle_message(typed_in("/effort xhigh", NAV_CHAT))
    assert runner.efforts["session-nav"] == "fierce"
    assert "gentle fierce" in api.sent[-1]["text"]


async def test_an_unenforced_choice_is_passed_through(tmp_path) -> None:
    """A model list is a hint. Refusing a name released this morning because it
    is absent from a list written months ago is worse than letting the runtime
    answer for itself."""
    channel, _, runner, _ = await wired(tmp_path)
    runner.options = lambda session_id=None: {
        "model": (("opus", "haiku"), False),
        "effort": ((), True),
    }

    await channel._handle_message(typed_in("/model something-brand-new", NAV_CHAT))

    assert runner.models["session-nav"] == "something-brand-new"


# --- the bot-self regression ------------------------------------------------
#
# Adding Codex sent every message to the bot's own chat instead of the seat's
# group, twice over: routing went by role, which no longer identifies a seat
# once two drivers exist, and the session name that does identify it was not
# passed down the relay. Both were fixed by hand and neither had a test, so the
# next refactor would have quietly done it again. These are that test.


FOUR_SEATS_ENV = {
    "HALYARD_SEATS": "nav,drv,xnav,xdrv",
    "HALYARD_SEAT_NAV": "runtime=claude-code session=claude-nav chat=-2001 role=navigator",
    "HALYARD_SEAT_DRV": "runtime=claude-code session=claude-drv chat=-2002 role=driver",
    "HALYARD_SEAT_XNAV": "runtime=codex session=codex-nav chat=-2003 role=navigator",
    "HALYARD_SEAT_XDRV": "runtime=codex session=codex-drv chat=-2004 role=driver",
}


async def four_seats(tmp_path: Path):
    from halyard.core.seats import from_environment

    store = ApprovalStore(ttl=timedelta(minutes=5))
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        # A distinct default, so "landed in the default chat" is unmistakable:
        # it is the one destination none of the four seats owns.
        chat_id="-9999-DEFAULT",
        authorized_user_ids=frozenset({APPROVER}),
        seats=from_environment(FOUR_SEATS_ENV),
    )
    return channel, api, store


SEAT_CASES = [
    ("claude-nav", "claude-code", Role.NAVIGATOR, "-2001"),
    ("claude-drv", "claude-code", Role.DRIVER, "-2002"),
    ("codex-nav", "codex", Role.NAVIGATOR, "-2003"),
    ("codex-drv", "codex", Role.DRIVER, "-2004"),
]


@pytest.mark.parametrize(("session_name", "agent_id", "role", "chat"), SEAT_CASES)
async def test_a_card_never_lands_in_the_bots_own_chat(
    tmp_path: Path, session_name: str, agent_id: str, role: Role, chat: str
) -> None:
    """The regression, from the approval-card side.

    Two seats share the role `driver`; only the session name and the runtime
    tell a Claude card from a Codex one. A card that cannot be placed falls to
    the default chat, which is the bot talking to itself — the exact symptom.
    """
    channel, api, store = await four_seats(tmp_path)
    request = await an_approval(store, agent_id=agent_id, role=role, session_name=session_name)

    await channel.send_approval_request(request)

    assert api.sent[0]["chat_id"] == chat
    assert api.sent[0]["chat_id"] != "-9999-DEFAULT"


@pytest.mark.parametrize(("session_name", "agent_id", "role", "chat"), SEAT_CASES)
async def test_a_reply_never_lands_in_the_bots_own_chat(
    tmp_path: Path, session_name: str, agent_id: str, role: Role, chat: str
) -> None:
    """The same regression from the relay side, which is where it actually bit.

    The relay has to carry the session name all the way to the channel; the bug
    was that it passed only the role, so every reply from either driver went to
    the default chat.
    """
    channel, api, _ = await four_seats(tmp_path)

    await channel.send_message(
        session_name, "done", None, agent_id=agent_id, session_name=session_name
    )

    assert api.sent[0]["chat_id"] == chat
    assert api.sent[0]["chat_id"] != "-9999-DEFAULT"


async def test_the_two_codex_seats_do_not_collapse_into_one(tmp_path: Path) -> None:
    """A Codex navigator and a Codex driver share a runtime and differ only by
    session. If routing ignored the session they would both answer to whichever
    seat it checked first."""
    channel, api, _ = await four_seats(tmp_path)

    await channel.send_message(
        "codex-nav", "a", Role.NAVIGATOR, agent_id="codex", session_name="codex-nav"
    )
    await channel.send_message(
        "codex-drv", "b", Role.DRIVER, agent_id="codex", session_name="codex-drv"
    )

    assert [m["chat_id"] for m in api.sent] == ["-2003", "-2004"]


async def test_one_name_in_two_runtimes_routes_by_runtime(tmp_path: Path) -> None:
    """The defect this is a regression for, and it is the ordinary case.

    A person who names two seats for one job names them the same thing:
    `alpha-engine-driver` was a Claude Code session *and* an Antigravity
    conversation at once. Routing matched the bare name, took the first seat
    listed, and delivered Antigravity's reply into the Claude driver's group —
    with nothing in the message saying it had gone to the wrong runtime.

    A session address is `(runtime, session_id)`, never a bare id. That rule was
    already written down in this repository, from the Codex postmortem, and this
    is the path that did not follow it.
    """
    from halyard.core.seats import Seat

    channel, api, _ = await routed(tmp_path)
    channel._seats = [
        Seat("drv", "claude-code", "alpha-engine-driver", DRV_CHAT, Role.DRIVER),
        Seat("gdrv", "antigravity", "alpha-engine-driver", "-1004444444444", Role.DRIVER),
    ]

    await channel.send_message(
        "conv-1",
        "done",
        Role.DRIVER,
        agent_id="antigravity",
        session_name="alpha-engine-driver",
    )

    assert api.sent[0]["chat_id"] == "-1004444444444"


async def test_the_claude_seat_of_that_pair_still_routes_to_its_own_group(tmp_path: Path) -> None:
    """Asserted beside it: a fix that sent everything to the *other* runtime
    would satisfy the test above perfectly well."""
    from halyard.core.seats import Seat

    channel, api, _ = await routed(tmp_path)
    channel._seats = [
        Seat("drv", "claude-code", "alpha-engine-driver", DRV_CHAT, Role.DRIVER),
        Seat("gdrv", "antigravity", "alpha-engine-driver", "-1004444444444", Role.DRIVER),
    ]

    await channel.send_message(
        "session-1",
        "done",
        Role.DRIVER,
        agent_id="claude-code",
        session_name="alpha-engine-driver",
    )

    assert api.sent[0]["chat_id"] == DRV_CHAT


async def test_a_failed_delivery_says_why_on_the_phone(tmp_path: Path, monkeypatch) -> None:
    """The reason the machine printed, carried to the person who asked.

    A Mac mini's `claude` CLI answered "Not logged in · Please run /login" and
    Halyard said to check the control plane's log — which is on the machine
    they had walked away from. The CLI writes that line to stdout, and the
    runner was reading only stderr, so the log said `failed (exit 1):` with
    nothing after the colon.
    """
    channel, api, _ = await routed(tmp_path)

    class Refuses:
        id = "claude-code"

        def last_error(self, _session_id):
            return "Not logged in · Please run /login"

        async def send(self, *_args, **_kwargs):
            return False

    await channel._deliver(
        _SessionTarget("s-1", "alpha-engine", None, Refuses()), "go", "tg:1", DRV_CHAT
    )

    said = api.sent[-1]["text"]
    assert "Not logged in" in said
    assert "s-1" in said


async def test_a_failure_with_no_output_says_that_instead(tmp_path: Path, monkeypatch) -> None:
    """Silence is a finding too, and must not be reported as a blank reason."""
    channel, api, _ = await routed(tmp_path)

    class Silent:
        id = "codex"

        def last_error(self, _session_id):
            return None

        async def send(self, *_args, **_kwargs):
            return False

    await channel._deliver(
        _SessionTarget("s-2", "alpha-engine", None, Silent()), "go", "tg:1", DRV_CHAT
    )

    assert "Nothing was printed" in api.sent[-1]["text"]


# --- the command list Telegram shows ------------------------------------------


async def test_the_commands_are_registered_on_start(tmp_path: Path) -> None:
    """Without this the bot still answers everything — Telegram does not need
    telling what a bot understands. What it changes is that typing `/` produces
    a menu instead of nothing, which on a phone is the difference between
    picking a command and remembering it."""
    channel, api, _ = await routed(tmp_path)
    await channel.start()
    await channel.stop()

    assert api.commands, "nothing was published"
    assert {name for name, _ in api.commands} == {name for name, _ in adapter.COMMANDS}


async def test_the_help_text_lists_exactly_those(tmp_path: Path) -> None:
    """One list, because there were two: a hand-written `/help` and nothing
    registered at all. Two lists drift, and the one that drifts is the one
    nobody re-reads."""
    channel, api, _ = await routed(tmp_path)

    await channel._handle_message(typed_in("/help", DRV_CHAT))

    said = api.sent[-1]["text"]
    for name, description in adapter.COMMANDS:
        assert f"/{name} — {description}" in said


async def test_a_refused_registration_does_not_stop_the_gate(tmp_path: Path) -> None:
    """Trading the thing for the label on it. The bot answers every command
    whether Telegram knows about them or not."""
    channel, api, _ = await routed(tmp_path)

    async def refuse(_commands):
        raise RuntimeError("Bad Request: too many commands")

    api.set_my_commands = refuse

    await channel.start()
    await channel.stop()

    assert channel._poller is None, "started and stopped cleanly"


# --- sending to a seat that is not this one -----------------------------------


async def with_two_seats(tmp_path: Path):
    from halyard.core.seats import Seat

    channel, api, _ = await routed(tmp_path)
    channel._seats = [
        Seat("drv", "claude-code", "alpha-driver", DRV_CHAT, Role.DRIVER),
        Seat("xnav", "codex", "alpha-xnav", "-1003333333333", Role.NAVIGATOR),
    ]
    return channel, api


async def test_a_message_can_be_aimed_at_another_seat(tmp_path: Path) -> None:
    """Eight groups is what a person ends up with — a navigator and a driver
    per runtime, per machine. Handing a question to a different agent meant
    finding its group and retyping the question in it."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav look at the failing test", DRV_CHAT))

    # The seat's own chat gets the message, not the one it was typed in.
    assert any(sent["chat_id"] == "-1003333333333" for sent in api.sent)


async def test_the_chat_it_was_typed_in_is_told_where_it_went(tmp_path: Path) -> None:
    """The person sending it is not looking at the seat they sent it to."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav carry on", DRV_CHAT))

    here = [sent for sent in api.sent if sent["chat_id"] == DRV_CHAT]
    assert any("xnav" in sent["text"] for sent in here)


async def test_the_receiving_chat_says_where_it_came_from(tmp_path: Path) -> None:
    """A seat's conversation should not acquire a message from nowhere."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav carry on", DRV_CHAT))

    there = [sent for sent in api.sent if sent["chat_id"] == "-1003333333333"]
    assert any("via another chat" in sent["text"] for sent in there)


async def test_an_unknown_seat_answers_with_the_list(tmp_path: Path) -> None:
    """The moment you need the list is the moment you got the name wrong, so
    it arrives then rather than behind a second command."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to nope hello", DRV_CHAT))

    said = api.sent[-1]["text"]
    assert "No seat called" in said
    assert "xnav" in said and "drv" in said


async def test_naming_a_seat_without_a_message_asks_for_one(tmp_path: Path) -> None:
    """Half a command is not an instruction to guess at the other half — but it
    is enough to ask the other half for, with the reply box already open."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))

    assert api.sent[-1]["text"] == "Send what to xnav?"
    assert api.sent[-1]["reply_markup"]["force_reply"] is True


async def test_answering_that_question_sends_it(tmp_path: Path) -> None:
    """`force_reply` is what makes this stateless: the answer arrives with the
    question attached, and the question is the only place the seat was written
    down."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(replying("look at this", "Send what to xnav?", DRV_CHAT))

    there = [sent for sent in api.sent if sent["chat_id"] == "-1003333333333"]
    assert any("look at this" in sent["text"] for sent in there)


async def test_a_bare_to_offers_the_seats(tmp_path: Path) -> None:
    """Two taps and no memory: which seat, then what to send."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to", DRV_CHAT))

    keyboard = api.sent[-1].get("reply_markup")
    assert [b["text"] for b in keyboard["inline_keyboard"][0]] == ["drv", "xnav"]


async def test_plain_text_that_answers_nothing_still_goes_to_this_seat(tmp_path: Path) -> None:
    """A message that is not answering one of those questions is what it always
    was: a message to the seat that owns this chat."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(replying("carry on", "some other message", DRV_CHAT))

    assert not any(sent["chat_id"] == "-1003333333333" for sent in api.sent)


async def test_plain_text_still_goes_to_this_chats_own_seat(tmp_path: Path) -> None:
    """The property worth protecting: this adds a second way in and changes
    nothing about the first."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("just carry on", DRV_CHAT))

    assert not any(sent["chat_id"] == "-1003333333333" for sent in api.sent)


# --- picking a model or an effort without typing it ---------------------------


async def choosing(tmp_path: Path, *, allowed=("flash_lite", "flash", "pro")):
    """A chat whose seat's runtime offers a closed set to pick from."""
    channel, api, _ = await routed(tmp_path)

    class Offering:
        id = "antigravity"

        def options(self, _session_id=None):
            return {"model": (allowed, False), "effort": ((), True)}

        def preferences(self, _session_id):
            return None, None

        def set_model(self, session_id, value):
            self.set = (session_id, value)

        set_effort = set_model

        def resolve(self, _name):
            return None

    from halyard.core.seats import Seat

    runner = Offering()
    channel._runners = {"antigravity": runner}
    channel._runner = runner
    channel._seats = [Seat("drv", "antigravity", "a-conversation", DRV_CHAT, Role.DRIVER)]

    class Resolves:
        session_id = "a-conversation"
        cwd = None
        model = None
        effort = None
        named_by_a_person = True

    runner.resolve = lambda _name: Resolves()
    return channel, api, runner


async def test_asking_without_a_value_offers_the_ones_it_accepts(tmp_path: Path) -> None:
    """The list is already known — the runtime is asked for it — and typing a
    name out on a phone to pick from three is work nobody needs to do."""
    channel, api, _ = await choosing(tmp_path)

    await channel._handle_message(typed_in("/model", DRV_CHAT))

    keyboard = api.sent[-1].get("reply_markup")
    assert keyboard is not None
    assert [b["text"] for b in keyboard["inline_keyboard"][0]] == ["flash_lite", "flash", "pro"]


async def test_an_empty_set_offers_no_keyboard(tmp_path: Path) -> None:
    """Antigravity has no notion of reasoning effort. A keyboard with no keys
    would suggest the question failed rather than that the answer is none."""
    channel, api, _ = await choosing(tmp_path)

    await channel._handle_message(typed_in("/effort", DRV_CHAT))

    assert api.sent[-1].get("reply_markup") is None


async def test_pressing_one_sets_it(tmp_path: Path) -> None:
    channel, _api, runner = await choosing(tmp_path)

    await channel._handle_callback(
        {
            "id": "q1",
            "from": {"id": int(APPROVER)},
            "message": {"chat": {"id": DRV_CHAT}},
            "data": "hc:model:pro",
        }
    )

    assert runner.set[1] == "pro"


async def test_a_stranger_cannot_press_it(tmp_path: Path) -> None:
    """Setting the model for a seat is not an approval, but it is still an
    action on somebody else's session — and the button is visible to a whole
    group."""
    channel, _api, runner = await choosing(tmp_path)

    await channel._handle_callback(
        {
            "id": "q1",
            "from": {"id": 999999},
            "message": {"chat": {"id": DRV_CHAT}},
            "data": "hc:model:pro",
        }
    )

    assert not hasattr(runner, "set")


async def test_an_approval_button_is_not_read_as_a_choice(tmp_path: Path) -> None:
    """Two prefixes, kept apart. An approval carries a nonce because it answers
    a question that must not be answerable twice; a preference has no such
    state and must not borrow that machinery."""
    from halyard.channels.telegram import cards

    assert cards.parse_choice_data("hf:handle:nonce:allow") is None
    assert cards.parse_callback_data("hc:model:pro") is None


# --- handing a message over without retyping it -------------------------------


def replying(text: str, to: str, chat: str, *, message_id: int = 7) -> dict:
    """A `/to …` sent as a reply to a message somebody wants handed over."""
    return {
        "message_id": 9,
        "from": {"id": int(APPROVER)},
        "chat": {"id": chat},
        "text": text,
        "reply_to_message": {"message_id": message_id, "text": to},
    }


async def test_replying_to_a_message_sends_that_message(tmp_path: Path) -> None:
    """The natural gesture for "this one, over there", and the text never has
    to be retyped — Telegram already carries it."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(
        replying("/to xnav", "the failing test is in test_wiring", DRV_CHAT)
    )

    there = [sent for sent in api.sent if sent["chat_id"] == "-1003333333333"]
    assert any("the failing test is in test_wiring" in sent["text"] for sent in there)


async def test_what_was_typed_beats_what_was_replied_to(tmp_path: Path) -> None:
    """Somebody who wrote out a message meant that one. Sending the other
    instead would be the worst kind of helpful."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(
        replying("/to xnav actually look at this", "the old thing", DRV_CHAT)
    )

    there = [sent for sent in api.sent if sent["chat_id"] == "-1003333333333"]
    assert any("actually look at this" in sent["text"] for sent in there)
    assert not any("the old thing" in sent["text"] for sent in there)


async def test_a_reply_without_a_seat_offers_the_buttons(tmp_path: Path) -> None:
    """The text is known and the seat is not — the one case a button can
    finish on its own, because nothing has to be remembered between the press
    and the message."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(replying("/to", "hand this over", DRV_CHAT, message_id=42))

    last = api.sent[-1]
    assert [b["text"] for b in last["reply_markup"]["inline_keyboard"][0]] == ["drv", "xnav"]
    # Attached to the message that holds the text, so the press can read it
    # back exactly rather than from the shortened preview.
    assert last["reply_to_message_id"] == 42


async def test_pressing_a_seat_sends_the_message_it_hangs_off(tmp_path: Path) -> None:
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_callback(
        {
            "id": "q1",
            "from": {"id": int(APPROVER)},
            "message": {
                "chat": {"id": DRV_CHAT},
                "reply_to_message": {"text": "hand this over"},
            },
            "data": "hc:to:xnav",
        }
    )

    there = [sent for sent in api.sent if sent["chat_id"] == "-1003333333333"]
    assert any("hand this over" in sent["text"] for sent in there)


async def test_a_press_with_nothing_to_carry_asks_for_it(tmp_path: Path) -> None:
    """The seat was picked from a bare `/to`, so there is no message yet —
    which is a question to ask, not an error."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_callback(
        {
            "id": "q1",
            "from": {"id": int(APPROVER)},
            "message": {"chat": {"id": DRV_CHAT}},
            "data": "hc:to:xnav",
        }
    )

    assert api.sent[-1]["text"] == "Send what to xnav?"
    assert not any(sent["chat_id"] == "-1003333333333" for sent in api.sent)


async def test_the_list_is_written_to_every_scope_that_can_shadow_it(tmp_path: Path) -> None:
    """Telegram shows the narrowest scope that has a list, and a person is
    almost always an administrator of the groups they made.

    Measured on a live bot, twice. Writing the default fixed nothing, because
    `all_group_chats` held an older list. Writing two of the three fixed
    nothing either, because `all_chat_administrators` is narrower than both and
    was the one left out — so a command answered when typed and never appeared
    in the menu.
    """
    from halyard.channels.telegram.api import TelegramApi

    written: list[dict | None] = []

    class Recording(TelegramApi):
        def __init__(self) -> None: ...

        async def _call(self, method: str, **payload):
            if method == "setMyCommands":
                written.append(payload.get("scope"))
            return {}

    await Recording().set_my_commands((("chat", "x"),))

    kinds = {scope["type"] if scope else "default" for scope in written}
    assert kinds == {
        "default",
        "all_private_chats",
        "all_group_chats",
        "all_chat_administrators",
    }


async def test_a_picked_seat_holds_even_when_the_reply_is_not_attached(tmp_path: Path) -> None:
    """`force_reply` only *asks* the client to attach the question, and a client
    that does not is indistinguishable from somebody talking to this chat.

    Twice, measured live, that sent a message to the seat that owns the chat
    instead of the one that had just been picked — the agent got a question
    meant for somebody else and said so. So the seat is held for a few minutes
    as well, and either signal is enough.
    """
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))
    # No `reply_to_message`: exactly what arrives when the box never opened.
    await channel._handle_message(typed_in("look at this", DRV_CHAT))

    assert any("sent to <b>xnav</b>" in sent["text"] for sent in api.sent)


async def test_a_held_seat_covers_one_message_and_no_more(tmp_path: Path) -> None:
    """A tap redirects the sentence it was made for. If it kept redirecting,
    one press would quietly move a whole conversation somewhere else."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))
    await channel._handle_message(typed_in("look at this", DRV_CHAT))
    api.sent.clear()
    await channel._handle_message(typed_in("and this stays here", DRV_CHAT))

    assert not any("sent to <b>xnav</b>" in sent["text"] for sent in api.sent)


async def test_a_command_drops_a_seat_nobody_finished_picking(tmp_path: Path) -> None:
    """Typing a command instead of the sentence means it never came, and a
    hand-off left lying around would attach itself to something said later."""
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))
    await channel._handle_message(typed_in("/status", DRV_CHAT))
    api.sent.clear()
    await channel._handle_message(typed_in("unrelated", DRV_CHAT))

    assert not any("sent to <b>xnav</b>" in sent["text"] for sent in api.sent)


async def test_a_held_seat_belongs_to_the_person_who_picked_it(tmp_path: Path) -> None:
    """In a group, somebody else typing must not be swept into a hand-off they
    did not ask for."""
    channel, api = await with_two_seats(tmp_path)
    # Authorized too, so this asserts about the hand-off rather than about
    # being turned away at the door.
    somebody_else = "9393"
    channel._authorized = frozenset({APPROVER, somebody_else})

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))
    api.sent.clear()
    await channel._handle_message(typed_in("mine", DRV_CHAT, user=somebody_else))

    assert not any("sent to <b>xnav</b>" in sent["text"] for sent in api.sent)


async def test_the_reply_box_opens_for_whoever_pressed(tmp_path: Path) -> None:
    """`selective` limits a forced reply to people named in the text or the
    author of the message being replied to.

    This question names nobody and replies to nothing, so with that flag set it
    opened the reply box for no one at all — and the answer arrived as ordinary
    text, attached to nothing, and went to the seat that owns the chat instead
    of the one that had just been picked. A message reaching an agent nobody
    chose is the failure this whole flow is arranged to prevent.
    """
    channel, api = await with_two_seats(tmp_path)

    await channel._handle_message(typed_in("/to xnav", DRV_CHAT))

    markup = api.sent[-1]["reply_markup"]
    assert markup["force_reply"] is True
    assert "selective" not in markup
