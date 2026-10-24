"""The card desk: every card with buttons closes — when it is pressed, when what
it asks ends, or at its deadline — and it closes once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_telegram import APPROVER, CHAT, FakeTelegramApi, an_approval, press

from halyard.channels.telegram import cards
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.channels.telegram.desk import STANDING, CardDesk, kind_of
from halyard.channels.telegram.shelf import Kept, Shelf
from halyard.core.approvals import ApprovalStore, ResolutionReason
from halyard.core.audit import AuditAction, AuditLog, JsonlAuditSink
from halyard.core.questions import Choice, QuestionStore

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class RefusingApi(FakeTelegramApi):
    """Takes cards, and will not take a card's text back — only its buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.markups: list[dict] = []

    async def edit_message_text(self, *args, **kwargs):
        raise RuntimeError("Bad Request: message is too long")

    async def edit_message_reply_markup(self, chat_id, message_id, *, reply_markup=None):
        self.markups.append({"message_id": message_id, "reply_markup": reply_markup})


def picker(what: str = "label", value: str = "andon") -> dict:
    return {
        "inline_keyboard": [
            [{"text": value, "callback_data": f"hc:{what}:{value}"}],
            [dict(cards.CANCEL)],
        ]
    }


def edits_of(api: FakeTelegramApi, message_id: int) -> list[dict]:
    return [edit for edit in api.edits if edit["message_id"] == message_id]


# --- the desk ------------------------------------------------------------------


async def test_a_message_with_buttons_is_kept_and_one_without_is_not() -> None:
    desk = CardDesk(FakeTelegramApi(), Clock())

    card = await desk.open(CHAT, "Which label?", picker())
    plain = await desk.open(CHAT, "Done.", None)
    forced = await desk.open(CHAT, "Say it:", {"force_reply": True})

    assert card is not None and card.kind == "label"
    assert plain is None and forced is None
    assert desk.live() == [card]


async def test_a_message_about_something_is_kept_without_buttons() -> None:
    """A question with no option that fits a button still asks until it is
    answered or its deadline passes."""
    desk = CardDesk(FakeTelegramApi(), Clock())

    card = await desk.open(CHAT, "Which?", None, kind="question", key="ask_1")

    assert card is not None
    assert desk.of("question", "ask_1") == [card]


async def test_a_closed_card_keeps_what_it_said_and_loses_its_buttons() -> None:
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock())
    card = await desk.open(CHAT, "<b>#14</b> — Which label?", picker())

    assert await desk.close_card(card, outcome="☑️ andon")

    assert edits_of(api, card.message_id) == [
        {
            "chat_id": CHAT,
            "message_id": card.message_id,
            "text": "<b>#14</b> — Which label?\n\n☑️ andon",
            "reply_markup": None,
        }
    ]
    assert desk.live() == []


async def test_a_card_closes_once_however_many_paths_end_it() -> None:
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock())
    card = await desk.open(CHAT, "Which label?", picker())

    first = await desk.close_card(card, outcome="☑️ andon")
    again = await desk.close(CHAT, card.message_id, outcome="✖️ Cancelled")
    unknown = await desk.close(CHAT, 77, outcome="☑️ x", shown="old")
    unknown_again = await desk.close(CHAT, 77, outcome="☑️ x", shown="old")

    assert (first, again, unknown, unknown_again) == (True, False, True, False)
    assert len(api.edits) == 2


async def test_a_card_from_before_a_restart_closes_from_what_its_press_carried() -> None:
    """The desk never saw it, so what the button press carries of its text is
    all there is to keep — escaped, since it arrives as plain text."""
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock())

    await desk.close(CHAT, 77, outcome="☑️ andon", shown="Which <label>?")

    assert edits_of(api, 77)[0]["text"] == "Which &lt;label&gt;?\n\n☑️ andon"


async def test_ending_a_subject_closes_every_card_about_it_in_its_own_words() -> None:
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock())

    def ending(how: str, by: str | None) -> str:
        return f"{how} by {by}"

    here = await desk.open(CHAT, "Q", picker(), kind="approval", key="req_1", ending=ending)
    there = await desk.open("-1009", "Q", picker(), kind="approval", key="req_1", ending=ending)
    other = await desk.open(CHAT, "Q", picker(), kind="approval", key="req_2", ending=ending)

    assert await desk.end_about("approval", "req_1", "allow", "tg:1") == 2

    assert [edit["text"] for edit in api.edits] == ["allow by tg:1", "allow by tg:1"]
    assert {edit["message_id"] for edit in api.edits} == {here.message_id, there.message_id}
    assert desk.live() == [other]


async def test_a_card_past_its_deadline_is_closed_by_the_sweep() -> None:
    api = FakeTelegramApi()
    clock = Clock()
    desk = CardDesk(api, clock)
    deadline = NOW + timedelta(minutes=5)
    asked = await desk.open(
        CHAT, "Q", picker(), key="k", expires_at=deadline, ending=lambda how, by: f"⌛ {how}"
    )
    offered = await desk.open(CHAT, "Pick", picker(), expires_at=deadline)
    lasting = await desk.open(CHAT, "Pick", picker())

    assert await desk.sweep() == 0
    clock.at = deadline

    assert await desk.sweep() == 2
    assert edits_of(api, asked.message_id)[0]["text"] == "⌛ expired"
    assert edits_of(api, offered.message_id)[0]["text"] == "Pick\n\n⌛ Expired"
    assert desk.live() == [lasting]


async def test_when_telegram_refuses_the_text_the_buttons_still_go() -> None:
    api = RefusingApi()
    desk = CardDesk(api, Clock())
    card = await desk.open(CHAT, "Which label?", picker())

    assert await desk.close_card(card, outcome="☑️ andon")

    assert api.markups == [{"message_id": card.message_id, "reply_markup": {"inline_keyboard": []}}]
    assert desk.live() == []


def test_a_card_is_known_by_its_first_button() -> None:
    assert kind_of(picker("inspect", "proof")) == "inspect"
    assert kind_of({"inline_keyboard": [[{"text": "x", "callback_data": "hg:ab12cd34:m"}]]}) == (
        "commit"
    )
    assert kind_of({"force_reply": True}) == ""


# --- the channel's cards ------------------------------------------------------


@pytest.fixture
async def wired(tmp_path: Path):
    clock = Clock()
    store = ApprovalStore(ttl=timedelta(minutes=5), clock=clock)
    questions = QuestionStore(ttl=timedelta(minutes=5), clock=clock)
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        question_store=questions,
        authorized_user_ids=frozenset({APPROVER}),
        clock=clock,
        poll_retry_seconds=0.01,
    )
    try:
        yield channel, api, store, questions, clock, sink
    finally:
        await audit.close()


def tapped(data: str, message_id: int, text: str = "Which one?") -> dict:
    return {
        "id": "cb",
        "from": {"id": int(APPROVER)},
        "message": {"message_id": message_id, "chat": {"id": CHAT}, "text": text},
        "data": data,
    }


async def settled(channel: TelegramChannel) -> None:
    """Wait for what a press detached — an inspection, a workflow."""
    for _ in range(100):
        pending = [task for task in channel._sending if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def test_an_approval_nobody_answered_says_so_and_loses_its_buttons(wired) -> None:
    """What the deadline used to leave behind: a card still offering Allow and
    Deny, still counting down, on a command already denied."""
    channel, api, store, _, _, _ = wired
    request = await an_approval(store)
    card = int(await channel.send_approval_request(request))

    # What the wait for an answer does when nobody gives one.
    ended = await store.deny(request.request_id, reason=ResolutionReason.TIMEOUT, note="")
    await channel.approval_ended(request, ended)

    [closed] = edits_of(api, card)
    assert closed["reply_markup"] is None
    assert closed["text"].startswith("<b>⌛ EXPIRED — DENIED</b>")
    assert "docker compose down postgres" in closed["text"]


async def test_halyard_stopping_says_so_on_the_card(wired) -> None:
    channel, api, store, _, _, _ = wired
    request = await an_approval(store)
    card = int(await channel.send_approval_request(request))

    await store.shutdown()
    await channel.approval_ended(request, await store.resolution_of(request.request_id))

    [closed] = edits_of(api, card)
    assert closed["text"].startswith("<b>⏹ DENIED — HALYARD STOPPED</b>")


async def test_a_decided_approval_is_left_to_the_press_that_decided_it(wired) -> None:
    """The press closes its own card in its own words — allowed, by whom — and
    hearing afterwards that the request ended changes nothing."""
    channel, api, store, _, _, _ = wired
    request = await an_approval(store)
    card = int(await channel.send_approval_request(request))

    await channel._handle_callback(press(request, cards.ALLOW))
    await channel.approval_ended(request, await store.resolution_of(request.request_id))

    [closed] = edits_of(api, card)
    assert closed["text"].startswith("<b>✅ ALLOWED</b> by tg:")


async def test_a_card_nothing_reported_on_closes_at_its_deadline(wired) -> None:
    """The backstop: whatever forgot to close it, its deadline does."""
    channel, api, store, _, clock, _ = wired
    request = await an_approval(store)
    card = int(await channel.send_approval_request(request))

    await channel._close_expired()
    assert edits_of(api, card) == []
    clock.at = request.expires_at
    await channel._close_expired()

    [closed] = edits_of(api, card)
    assert closed["text"].startswith("<b>⌛ EXPIRED — DENIED</b>")


async def test_a_press_after_nobody_answered_is_too_late_not_a_replay(wired) -> None:
    """The first press, late. It was recorded as a second press of a decided
    card, and the card went on looking open."""
    channel, api, store, _, _, sink = wired
    request = await an_approval(store)
    card = int(await channel.send_approval_request(request))
    await store.deny(request.request_id, reason=ResolutionReason.TIMEOUT, note="")

    await channel._handle_callback(press(request, cards.ALLOW))

    assert api.answers[-1]["text"] == "Too late — that expired and was denied."
    assert AuditAction.REPLAYED_CALLBACK not in {r.action for r in await sink.read_all()}
    assert edits_of(api, card)[0]["text"].startswith("<b>⌛ EXPIRED — DENIED</b>")


async def test_the_buttons_on_a_request_this_process_never_saw_go(wired) -> None:
    channel, api, store, _, _, _ = wired
    request = await an_approval(store)
    pressed = press(request, cards.ALLOW)
    pressed["message"] = {"message_id": 90, "chat": {"id": CHAT}, "text": "PERMISSION REQUEST"}

    await channel._handle_callback(pressed)

    [closed] = edits_of(api, 90)
    assert closed["reply_markup"] is None
    assert "No longer open" in closed["text"]


async def test_a_question_nobody_answered_goes_back_to_the_terminal_and_says_so(wired) -> None:
    channel, api, _, questions, _, _ = wired
    request = await questions.create(
        session_id="s1",
        agent_id="claude-code",
        project="alpha-engine",
        question="Which?",
        options=[Choice(label="A"), Choice(label="B")],
    )
    card = int(await channel.send_question(request))

    ended = await questions.give_up(request.request_id, reason=ResolutionReason.TIMEOUT)
    await channel.question_ended(request, ended)

    [closed] = edits_of(api, card)
    assert closed["reply_markup"] is None
    assert "Left to the terminal — nobody answered in time" in closed["text"]


async def test_a_late_press_on_a_question_is_not_a_replay(wired) -> None:
    channel, api, _, questions, _, sink = wired
    request = await questions.create(
        session_id="s1",
        agent_id="claude-code",
        project="alpha-engine",
        question="Which?",
        options=[Choice(label="A")],
    )
    card = int(await channel.send_question(request))
    await questions.give_up(request.request_id, reason=ResolutionReason.TIMEOUT)

    await channel._handle_callback(tapped(cards.question_data(request, 0), card))

    assert api.answers[-1]["text"] == "Too late — that went back to the terminal."
    assert AuditAction.REPLAYED_CALLBACK not in {r.action for r in await sink.read_all()}
    assert "Left to the terminal" in edits_of(api, card)[0]["text"]


#: A button of every kind a card offers once, and what its card says after.
CHOICES = [
    ("label", "andon", "☑️ andon"),
    ("run", "test-fast", "☑️ test-fast"),
    ("inspect", "proof", "☑️ proof"),
    ("check", "proof", "☑️ proof"),
    ("flow", "deliver", "☑️ deliver"),
    ("fwd", "nav", "☑️ nav"),
    ("to", "nav", "☑️ nav"),
    ("open", "Claude", "☑️ Claude"),
    ("result", "proof>nav", "☑️ proof → nav"),
    ("model", "opus", "☑️ opus"),
    ("effort", "high", "☑️ high"),
    ("handoff", "review", "☑️ review"),
    ("handto", "back>xrev", "☑️ back → xrev"),
    ("do", "status", "☑️ /status"),
    ("cancel", "x", "✖️ Cancelled"),
]


@pytest.mark.parametrize(("what", "value", "said"), CHOICES)
async def test_every_choice_closes_its_card_once_pressed(wired, what, value, said) -> None:
    """Before what it chose is done, so a second press finds nothing to press.
    The inspection picker, `/forward` and `/to` were the ones that stayed —
    pressed again, they ran the inspection again and sent the reply twice."""
    channel, api, *_ = wired
    data = f"hc:{what}:{value}"
    card = await channel._say(
        "<b>Which</b> one?",
        CHAT,
        reply_markup={"inline_keyboard": [[{"text": value, "callback_data": data}]]},
    )

    await channel._handle_callback(tapped(data, card.message_id))
    await settled(channel)

    [closed] = edits_of(api, card.message_id)
    assert closed == {
        "chat_id": CHAT,
        "message_id": card.message_id,
        "text": f"<b>Which</b> one?\n\n{said}",
        "reply_markup": None,
    }
    assert channel._desk.live() == []


async def test_a_step_and_a_picked_label_say_what_their_button_said(wired) -> None:
    """Their values are places — a step's in its flow, a label's in its group —
    so the card says what the button showed."""
    channel, api, *_ = wired
    step = await channel._say(
        "Start at?", CHAT, reply_markup=cards.workflow_steps("deliver", ("develop",))
    )
    label = await channel._say(
        "Pick one", CHAT, reply_markup=cards.label_picks("c", "test-area", 0, ("area:total",))
    )

    await channel._handle_callback(tapped("hc:flowat:deliver>0", step.message_id))
    await channel._handle_callback(tapped("hc:pick:ctest-area>0>0", label.message_id))
    await settled(channel)

    assert edits_of(api, step.message_id)[0]["text"] == "Start at?\n\n☑️ 1 · develop"
    assert edits_of(api, label.message_id)[0]["text"] == "Pick one\n\n☑️ area:total"


#: A workflow run's own buttons, named here rather than read from `STANDING`,
#: so emptying that would fail this rather than leave it nothing to check.
RUN_BUTTONS = ("flowgo", "flowon", "flowpick", "flowstop", "flowto")


def test_every_run_button_is_standing() -> None:
    assert set(RUN_BUTTONS) == STANDING


@pytest.mark.parametrize("what", RUN_BUTTONS)
async def test_a_run_s_own_buttons_keep_their_card(wired, what) -> None:
    """The run goes on after them, and the card is how it is steered again."""
    channel, api, *_ = wired
    data = f"hc:{what}:deliver"
    card = await channel._say(
        "deliver stopped",
        CHAT,
        reply_markup={"inline_keyboard": [[{"text": "go", "callback_data": data}]]},
    )

    await channel._handle_callback(tapped(data, card.message_id))
    await settled(channel)

    assert edits_of(api, card.message_id) == []
    assert channel._desk.live() == [card]


async def test_somebody_else_pressing_a_choice_leaves_the_card_open(wired) -> None:
    channel, api, *_ = wired
    card = await channel._say("Which?", CHAT, reply_markup=picker())
    pressed = tapped("hc:label:andon", card.message_id)
    pressed["from"] = {"id": 9999}

    await channel._handle_callback(pressed)

    assert edits_of(api, card.message_id) == []
    assert channel._desk.live() == [card]


async def test_the_whole_way_a_request_nobody_answers_ends_with_its_card_closed(
    tmp_path: Path,
) -> None:
    """Core and the channel together: the card goes out, nobody answers, the
    request is denied — and the card says so, with no button left on it."""
    from test_service import ask, build_service

    store = ApprovalStore(ttl=timedelta(milliseconds=50))
    audit = AuditLog([JsonlAuditSink(tmp_path / "channel-audit.jsonl")])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=store,
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
    )
    service, _, sink = build_service(tmp_path, channel=channel, store=store)
    await sink.open()

    outcome = await ask(service)
    await asyncio.gather(*service._closing)

    assert not outcome.allowed
    [card] = api.sent
    assert card["reply_markup"]["inline_keyboard"], "it went out with its buttons"
    [closed] = api.edits
    assert closed["reply_markup"] is None
    assert closed["text"].startswith("<b>⌛ EXPIRED — DENIED</b>")
    assert channel._desk.live() == []
    await audit.close()


# --- across a restart -------------------------------------------------------------


def shelved(path: Path, *cards_kept) -> Shelf:
    shelf = Shelf(path)
    for kept in cards_kept:
        shelf.put(kept)
    return shelf


def kept_card(message_id: int, html: str = "<b>Which?</b>", *, at: datetime = NOW) -> Kept:
    return Kept(CHAT, message_id, "label", "", html, at)


def test_the_shelf_keeps_what_was_put_and_forgets_what_was_taken(tmp_path: Path) -> None:
    shelf = shelved(tmp_path / "halyard.db", kept_card(7), kept_card(8))

    shelf.take(CHAT, 7)

    assert shelf.everything() == [kept_card(8)]


def test_a_shelf_that_cannot_be_read_is_empty_rather_than_broken(tmp_path: Path) -> None:
    unreadable = tmp_path / "halyard.db"
    unreadable.write_text("not a database")

    assert Shelf(tmp_path / "none.db").everything() == []
    assert Shelf(unreadable).everything() == []
    Shelf(unreadable).put(kept_card(7))  # said in the log, never raised


async def test_a_card_is_written_down_as_it_opens_and_struck_off_as_it_closes(
    tmp_path: Path,
) -> None:
    shelf = Shelf(tmp_path / "halyard.db")
    desk = CardDesk(FakeTelegramApi(), Clock(), shelf=shelf)

    card = await desk.open(CHAT, "<b>Which?</b>", picker())
    [kept] = shelf.everything()
    await desk.close_card(card, outcome="☑️ andon")

    assert (kept.message_id, kept.html, kept.kind) == (card.message_id, "<b>Which?</b>", "label")
    assert shelf.everything() == []


async def test_what_the_last_process_left_open_is_closed_saying_so(tmp_path: Path) -> None:
    """Its cards stayed in the chat, buttons and all, answering any press with
    "no longer open" — the memory that knew them went with the process."""
    shelf = shelved(tmp_path / "halyard.db", kept_card(7), kept_card(8, "Run which one?"))
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock(), shelf=shelf, pace=0)

    assert await desk.close_left_over(await desk.left_over()) == 2

    assert [edit["text"] for edit in api.edits] == [
        "<b>Which?</b>\n\n⏹ <i>Closed — Halyard restarted.</i>",
        "Run which one?\n\n⏹ <i>Closed — Halyard restarted.</i>",
    ]
    assert all(edit["reply_markup"] is None for edit in api.edits)
    assert shelf.everything() == []


async def test_a_card_telegram_would_not_close_is_tried_again_for_a_while(tmp_path: Path) -> None:
    """At the next start — unless it has been tried for long enough, when a
    message deleted or out of the bot's reach would be tried forever."""
    recent = kept_card(7, at=NOW - timedelta(hours=1))
    old = kept_card(8, at=NOW - timedelta(days=3))
    shelf = shelved(tmp_path / "halyard.db", recent, old)

    class Unreachable(RefusingApi):
        async def edit_message_reply_markup(self, *args, **kwargs):
            raise RuntimeError("Bad Request: message to edit not found")

    desk = CardDesk(Unreachable(), Clock(), shelf=shelf, pace=0)

    assert await desk.close_left_over(await desk.left_over()) == 0
    assert shelf.everything() == [recent]


async def test_a_card_this_process_opened_is_never_taken_for_a_left_over_one(
    tmp_path: Path,
) -> None:
    shelf = Shelf(tmp_path / "halyard.db")
    api = FakeTelegramApi()
    desk = CardDesk(api, Clock(), shelf=shelf, pace=0)
    card = await desk.open(CHAT, "Which?", picker())

    await desk.close_left_over(shelf.everything())

    assert api.edits == []
    assert desk.live() == [card]


async def test_starting_closes_the_cards_the_last_run_left_open(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    shelved(database, kept_card(7, "<b>Allow?</b>"))
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeTelegramApi()
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        database=database,
        poll_retry_seconds=0.01,
    )
    channel._desk._pace = 0

    await channel.start()
    await settled(channel)
    await channel.stop()
    await audit.close()

    [closed] = api.edits
    assert closed["message_id"] == 7
    assert closed["text"] == "<b>Allow?</b>\n\n⏹ <i>Closed — Halyard restarted.</i>"
    assert Shelf(database).everything() == []
