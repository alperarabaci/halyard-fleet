"""Codex Desktop's waiting questions: read from the rollout, put on a phone, and
answered from either end.

The record shapes are the ones measured on a live Desktop thread — see
`halyard.agents.codex.questions`.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from halyard.agents.codex import questions
from halyard.agents.codex.runner import CodexRunner
from halyard.agents.codex.watching import transcript
from halyard.core.audit import AuditLog, JsonlAuditSink
from halyard.core.gate import Gate
from halyard.core.left_questions import LeftQuestions
from halyard.core.questions import QuestionStore
from halyard.core.registry import SessionRegistry
from halyard.core.transcripts import TranscriptWatcher

CALL = "call_0oKTXeRDNEgOdqyd4iUwDZgA"
QUESTION = questions.question_id(CALL, 0)
SESSION = "019f4846-4445-79b2-9faf-c8ff226de08d"


def asked(call: str = CALL, title: str = "Which format?", options=("PDF", "HTML")) -> list[str]:
    """One question, written both ways the Desktop app writes it."""
    question = {"title": title, **({"options": list(options)} if options else {})}
    return [
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": questions.TOOL,
                    "call_id": call,
                    "arguments": json.dumps({"questions": [question]}),
                },
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": call,
                        "delivery": "async",
                        "questions": [question],
                    },
                },
            }
        ),
    ]


def answer(answer: str = "PDF", question: str = QUESTION, title: str = "Which format?") -> str:
    text = questions.envelope(question, title, answer)
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


# --- reading them ---------------------------------------------------------------


def test_a_question_written_twice_is_one_question() -> None:
    reading = questions.read(asked())

    assert [(q.id, q.title, q.options) for q in reading.asked] == [
        (QUESTION, "Which format?", ("PDF", "HTML"))
    ]
    assert reading.answered == {}
    assert reading.set_aside is False


def test_a_question_without_options_takes_free_text() -> None:
    [found] = questions.read(asked(title="Which branch?", options=())).asked

    assert found.options == ()


def test_an_answer_names_its_question() -> None:
    reading = questions.read([answer("HTML")])

    assert reading.answered == {QUESTION: "HTML"}
    assert reading.set_aside is False, "an answer is not moving on"


def test_the_desk_s_own_record_of_an_answer_counts_too() -> None:
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [
                        {"type": "text", "text": questions.envelope(QUESTION, "Which?", "PDF")}
                    ],
                },
            },
        }
    )

    assert questions.read([line]).answered == {QUESTION: "PDF"}


def test_the_answer_is_the_envelope_the_desktop_writes() -> None:
    """Measured on a real answer: the id names the tool, the call and the
    question's place in it."""
    text = questions.envelope(QUESTION, "Which format?", "PDF")

    assert f'["request_user_input_async","{CALL}",0]' == QUESTION
    assert text.startswith(questions.OPEN + "\n") and text.endswith("\n" + questions.CLOSE)
    body = json.loads(text[len(questions.OPEN) : -len(questions.CLOSE)])
    assert body == [{"questionItemId": QUESTION, "question": "Which format?", "answer": "PDF"}]


def test_an_ordinary_message_is_not_an_answer() -> None:
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"text": "carry on"}]},
        }
    )

    reading = questions.read([line, "not json"])

    assert (reading.asked, reading.answered) == ((), {})


def said(text: str) -> str:
    """A user message, as the rollout records one typed in the chat."""
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        separators=(",", ":"),
    )


def test_writing_something_else_sets_the_questions_before_it_aside() -> None:
    """Measured: three questions nobody answered, each followed by a message
    the person typed, and the app no longer showing any of them."""
    reading = questions.read([*asked(), said("carry on with the tests")])

    assert reading.asked == ()
    assert reading.set_aside is True


def test_a_question_asked_after_the_message_is_still_open() -> None:
    reading = questions.read([said("carry on"), *asked()])

    assert [q.id for q in reading.asked] == [QUESTION]
    assert reading.set_aside is True


def test_what_codex_adds_as_a_user_message_is_not_the_person_moving_on() -> None:
    """Every one it adds starts with a tag; none the person wrote did."""
    reading = questions.read(
        [*asked(), said("<environment_context>\n  <cwd>/repo</cwd>"), said("<turn_aborted>")]
    )

    assert [q.id for q in reading.asked] == [QUESTION]
    assert reading.set_aside is False


# --- finding the rollout being written --------------------------------------------


def test_the_rollout_being_written_is_the_newest(tmp_path: Path) -> None:
    """The Desktop app moves a thread on to a new, suffixed rollout every so
    often; the first one stops growing."""
    day = tmp_path / "sessions" / "2026" / "10" / "08"
    day.mkdir(parents=True)
    first = day / f"rollout-2026-10-03T00-30-53-{SESSION}.jsonl"
    later = day / f"rollout-2026-10-08T18-27-42-{SESSION}_01a11c20-a0f4-7c23.jsonl"
    other = day / "rollout-2026-10-09T00-00-00-01a125fd-5abb-7612-8112-eb02a1e5d418.jsonl"
    for number, path in enumerate((first, later, other)):
        path.write_text("{}\n")
        os.utime(path, (1_000_000 + number, 1_000_000 + number))

    assert transcript(SESSION, tmp_path) == later


# --- carrying them to the phone and back -------------------------------------------


class Channel:
    def __init__(self) -> None:
        self.questions: list = []
        self.closed: list[tuple[str, str, str]] = []
        self.messages: list[str] = []

    async def send_question(self, request) -> str:
        self.questions.append(request)
        return "sent"

    async def close_question(self, request, *, answer: str, by: str) -> None:
        self.closed.append((request.request_id, answer, by))

    async def send_message(self, session_id, text, role=None, **_) -> str:
        self.messages.append(text)
        return "sent"


async def left(tmp_path: Path, *, delivered: bool = True, gate: Gate | None = None):
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    await audit.open()
    store = QuestionStore(ttl=timedelta(milliseconds=50))
    channel = Channel()
    given: list[tuple] = []

    async def deliver(request, said: str) -> bool:
        given.append((request.session_id, request.tool_use_id, request.question, said))
        return delivered

    carried = LeftQuestions(
        store=store,
        audit=audit,
        registry=SessionRegistry(),
        channel=channel,
        deliver=deliver,
        project="alpha-engine",
        gate=gate,
    )
    return carried, store, channel, given


async def ask(carried: LeftQuestions) -> None:
    [question] = questions.read(asked()).asked
    await carried.asked(session_id=SESSION, agent_id="codex", question=question)


async def settles() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_question_is_carded_once_and_does_not_expire(tmp_path: Path) -> None:
    """The question waits at the desk for as long as it takes; so does its card."""
    carried, store, channel, _ = await left(tmp_path)

    await ask(carried)
    await ask(carried)
    await asyncio.sleep(0.1)

    [card] = channel.questions
    assert card.expires_at is None
    assert [o.label for o in card.options] == ["PDF", "HTML"]
    assert [r.request_id for r in await store.list_open()] == [card.request_id]


async def test_an_answer_from_the_phone_is_handed_to_codex(tmp_path: Path) -> None:
    carried, store, channel, given = await left(tmp_path)
    await ask(carried)
    [card] = channel.questions

    await store.answer(card.request_id, nonce=card.nonce, answer="PDF", decided_by="tg:1")
    await settles()

    assert given == [(SESSION, QUESTION, "Which format?", "PDF")]
    assert channel.messages == []


async def test_an_answer_at_the_desk_closes_the_card(tmp_path: Path) -> None:
    carried, _store, channel, given = await left(tmp_path)
    await ask(carried)
    [card] = channel.questions

    await carried.answered(QUESTION, "HTML")
    await settles()

    assert channel.closed == [(card.request_id, "HTML", "at the desk")]
    assert given == []


async def test_the_phone_s_answer_coming_back_through_the_rollout_changes_nothing(
    tmp_path: Path,
) -> None:
    carried, store, channel, _ = await left(tmp_path)
    await ask(carried)
    [card] = channel.questions

    await store.answer(card.request_id, nonce=card.nonce, answer="PDF")
    await settles()
    await carried.answered(QUESTION, "PDF")

    assert channel.closed == []


async def test_an_answer_that_did_not_reach_codex_is_said(tmp_path: Path) -> None:
    carried, store, channel, _ = await left(tmp_path, delivered=False)
    await ask(carried)
    [card] = channel.questions

    await store.answer(card.request_id, nonce=card.nonce, answer="PDF")
    await settles()

    assert any("did not reach" in text for text in channel.messages)


async def test_moving_on_closes_the_card_unanswered(tmp_path: Path) -> None:
    carried, _store, channel, given = await left(tmp_path)
    await ask(carried)
    [card] = channel.questions

    await carried.set_aside("another-session")
    untouched = list(channel.closed)
    await carried.set_aside(SESSION)
    await settles()

    assert untouched == []
    assert channel.closed == [(card.request_id, None, "at the desk")]
    assert given == []


async def test_a_paused_gate_cards_nothing(tmp_path: Path) -> None:
    gate = Gate()
    await gate.pause("tg:1")
    carried, _store, channel, _ = await left(tmp_path, gate=gate)

    await ask(carried)

    assert channel.questions == []


# --- the watcher finding them --------------------------------------------------------


class Handler:
    """Where the watcher hands what it found: `LeftQuestions`, in the app."""

    def __init__(self) -> None:
        self.carded: list[str] = []
        self.closed: list[tuple[str, str]] = []
        self.set_aside_in: list[str] = []

    async def set_aside(self, session_id: str) -> None:
        self.set_aside_in.append(session_id)

    async def asked(self, **kwargs) -> None:
        self.carded.append(kwargs["question"].id)

    async def answered(self, question: str, said: str) -> None:
        self.closed.append((question, said))


def watching(tmp_path: Path, handler: Handler) -> TranscriptWatcher:
    return TranscriptWatcher(channel=Channel(), gate=Gate(), roots=(tmp_path,), questions=handler)


def rollout(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "sessions" / "2026" / "10" / "10" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


async def test_a_question_in_the_rollout_reaches_the_handler_and_so_does_its_answer(
    tmp_path: Path,
) -> None:
    handler = Handler()
    w = watching(tmp_path, handler)
    file = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    w.note(session_id=SESSION, agent_id="codex")

    append(file, *asked())
    await w.poll_once()
    append(file, answer("HTML"))
    await w.poll_once()

    assert handler.carded == [QUESTION]
    assert handler.closed == [(QUESTION, "HTML")]


async def test_a_question_answered_before_the_next_poll_is_not_carded(tmp_path: Path) -> None:
    handler = Handler()
    w = watching(tmp_path, handler)
    file = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    w.note(session_id=SESSION, agent_id="codex")

    append(file, *asked(), answer("PDF"))
    await w.poll_once()

    assert handler.carded == []


async def test_a_thread_that_moved_to_a_new_rollout_is_followed_from_its_start(
    tmp_path: Path,
) -> None:
    """Found again on the adoption timer; the new file begins afresh, so a
    question already in it is news."""
    handler = Handler()
    w = watching(tmp_path, handler)
    first = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    os.utime(first, (1_000_000, 1_000_000))
    w.note(session_id=SESSION, agent_id="codex")
    moved = rollout(tmp_path, f"rollout-2026-10-10T12-00-00-{SESSION}_01a11c20.jsonl")
    append(moved, *asked())

    w.note(session_id=SESSION, agent_id="codex", refresh=True)
    await w.poll_once()

    assert w._watched[SESSION].transcript == moved
    assert handler.carded == [QUESTION]


# --- answering through Codex --------------------------------------------------------


async def test_an_answer_is_queued_into_the_thread_and_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued, the way the app sends its own answers: no resume, no model, no
    settings — the question is open in the app, and an answer is not a turn."""
    started: list[list[str]] = []

    class Done:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def exec_(*arguments, **_):
        started.append(list(arguments))
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    binary = tmp_path / "codex"
    binary.write_text("")

    took = await CodexRunner(binary=str(binary)).answer_question(
        SESSION, QUESTION, "Which format?", "PDF"
    )

    assert took is True
    assert started == [
        [
            str(binary),
            "queue",
            "--thread",
            SESSION,
            "--message",
            questions.envelope(QUESTION, "Which format?", "PDF"),
        ]
    ]


async def test_a_question_waiting_before_watching_began_is_carded(tmp_path: Path) -> None:
    """Asked before Halyard started, and still waiting: found once, from the
    start of the file, while one the person moved on from is not."""
    handler = Handler()
    w = watching(tmp_path, handler)
    file = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    append(file, *asked(call="call_old"), said("never mind"), *asked())

    w.note(session_id=SESSION, agent_id="codex")
    await w.poll_once()
    await w.poll_once()

    assert handler.carded == [QUESTION]


async def test_a_line_longer_than_a_read_is_stepped_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: a 772 KB line held every poll at the same place, and two
    questions asked an hour later were never seen."""
    from halyard.core import transcripts

    monkeypatch.setattr(transcripts, "MAX_READ_BYTES", 1024)
    handler = Handler()
    w = watching(tmp_path, handler)
    file = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    w.note(session_id=SESSION, agent_id="codex")

    append(file, json.dumps({"type": "event_msg", "payload": {"output": "x" * 5000}}))
    append(file, *asked())
    for _ in range(4):
        await w.poll_once()

    assert handler.carded == [QUESTION]


async def test_writing_something_else_reaches_the_handler(tmp_path: Path) -> None:
    handler = Handler()
    w = watching(tmp_path, handler)
    file = rollout(tmp_path, f"rollout-2026-10-10T10-00-00-{SESSION}.jsonl")
    w.note(session_id=SESSION, agent_id="codex")

    append(file, said("do the other thing first"))
    await w.poll_once()

    assert handler.set_aside_in == [SESSION]
