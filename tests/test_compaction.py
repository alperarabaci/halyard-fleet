"""Tests for what a session is told after a compaction.

The shape of this feature was forced by measurement, and the tests say so: a
hook cannot make a model write anything, and `PreCompact` output is refused
outright — so the record is produced *about* a session rather than *by* it.

Weighted toward the failures. Everything here is a convenience, and a
convenience that can hold up a session's restart is worse than no convenience.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from halyard.core.compaction import Recorder, conversation_tail, for_seat, read
from halyard.core.seats import Seat


def transcript(path: Path, *turns: tuple[str, str]) -> Path:
    lines = [json.dumps({"type": "summary", "summary": "older"})]
    lines += [
        json.dumps({"type": who, "message": {"content": [{"type": "text", "text": text}]}})
        for who, text in turns
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeRunner:
    """A runtime that answers a one-shot turn."""

    def __init__(self, answer: str | None = "the record", delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.asked: list[str] = []
        self.models: list[str | None] = []

    async def ask(self, text: str, *, model: str | None = None, **_) -> str | None:
        self.asked.append(text)
        self.models.append(model)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer


def seat(**overrides) -> Seat:
    fields = {
        "label": "nav",
        "runtime": "claude-code",
        "session": "alpha-navigator",
        "project": "alpha-engine",
    }
    fields.update(overrides)
    return Seat(**fields)


# --- reading the conversation ------------------------------------------------


def test_the_conversation_is_read_as_plain_text(tmp_path: Path) -> None:
    path = transcript(
        tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl",
        ("user", "run the tests"),
        ("assistant", "712 passed"),
    )

    text = conversation_tail(path)

    assert "run the tests" in text and "712 passed" in text


def test_a_transcript_that_will_not_parse_yields_what_it_can(tmp_path: Path) -> None:
    """A line caught mid-write must not cost the whole record."""
    path = tmp_path / "t.jsonl"
    path.write_text(
        "{ not json\n"
        + json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
        )
        + "\n",
        encoding="utf-8",
    )

    assert "ok" in conversation_tail(path)


def test_a_missing_transcript_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    assert conversation_tail(tmp_path / "gone.jsonl") == ""


def test_an_api_error_entry_is_not_part_of_the_conversation(tmp_path: Path) -> None:
    """Those are the client's own synthetic messages, not something anybody said."""
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": "529 Overloaded"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert conversation_tail(path) == ""


# --- what a seat is handed afterwards ----------------------------------------


def test_a_seat_with_a_file_gets_its_contents(tmp_path: Path) -> None:
    orientation = tmp_path / "post.md"
    orientation.write_text("read the queue first", encoding="utf-8")
    seats = [seat(after_compaction=str(orientation))]

    found = for_seat(
        seats,
        agent_id="claude-code",
        session_name="alpha-navigator",
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
    )

    assert found == "read the queue first"


def test_a_seat_without_one_is_handed_nothing(tmp_path: Path) -> None:
    """The common case. A driver running one command has no use for a page."""
    assert (
        for_seat(
            [seat()],
            agent_id="claude-code",
            session_name="alpha-navigator",
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
        )
        is None
    )


def test_a_file_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    seats = [seat(after_compaction=str(tmp_path / "missing.md"))]

    assert (
        for_seat(
            seats,
            agent_id="claude-code",
            session_name="alpha-navigator",
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
        )
        is None
    )


def test_an_unresolved_session_is_handed_nothing(tmp_path: Path) -> None:
    """A seat is `(runtime, session)`. Handing a Codex session the Claude
    navigator's orientation would be worse than handing it none."""
    orientation = tmp_path / "post.md"
    orientation.write_text("claude only", encoding="utf-8")
    seats = [seat(after_compaction=str(orientation))]

    assert (
        for_seat(
            seats,
            agent_id="codex",
            session_name="alpha-navigator",
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
        )
        is None
    )


def test_an_enormous_file_is_bounded(tmp_path: Path) -> None:
    big = tmp_path / "post.md"
    big.write_text("x" * 100_000, encoding="utf-8")

    assert len(read(big) or "") <= 32_000


# --- writing the record -------------------------------------------------------


async def test_the_record_is_written_and_handed_back(tmp_path: Path) -> None:
    instructions = tmp_path / "pre.md"
    instructions.write_text("write down the measured numbers", encoding="utf-8")
    transcript(
        tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl",
        ("assistant", "712 passed, I ran it"),
    )
    runner = FakeRunner("AKTIF IS: 712 passed — measured")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    written = await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert written
    assert (
        await recorder.take("9f1c2b3a-0000-0000-0000-000000000000")
        == "AKTIF IS: 712 passed — measured"
    )
    # The instructions and the conversation both reached the one-shot turn.
    assert "write down the measured numbers" in runner.asked[0]
    assert "712 passed" in runner.asked[0]


async def test_the_record_is_written_by_the_cheap_model(tmp_path: Path) -> None:
    """A distillation of text somebody else already wrote. The reasoning
    happened in the session, not here."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert runner.models == ["sonnet"]


async def test_a_long_record_is_trimmed_before_it_is_carried(tmp_path: Path) -> None:
    """It goes into a context that was just emptied on purpose. Carrying a long
    one across would refill what the compaction had cleared."""
    import halyard.core.compaction as compaction

    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("y" * 50_000)},
    )

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert (
        len(await recorder.take("9f1c2b3a-0000-0000-0000-000000000000") or "")
        <= compaction.RECORD_LIMIT
    )


async def test_the_prompt_asks_for_brevity_as_well(tmp_path: Path) -> None:
    """A model told to be brief writes something better than one whose answer
    is cut off, so the limit is asked for and enforced."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert "characters" in runner.asked[0]


async def test_the_record_is_handed_over_once(tmp_path: Path) -> None:
    """It describes one compaction. Leaving it behind would hand it to the next
    one as though it were fresh."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("once")},
    )
    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert await recorder.take("9f1c2b3a-0000-0000-0000-000000000000") == "once"
    assert await recorder.take("9f1c2b3a-0000-0000-0000-000000000000") is None


async def test_a_seat_without_instructions_writes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    recorder = Recorder(roots=(tmp_path,), seats=[seat()], runners={"claude-code": runner})

    written = await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert written is False
    assert runner.asked == []


async def test_a_runtime_that_cannot_run_one_shot_turns_is_skipped(tmp_path: Path) -> None:
    """Only Claude Code has `ask`. A seat on another runtime is left alone
    rather than reached for with a method it does not have."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": object()},
    )

    assert (
        await recorder.write(
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
            agent_id="claude-code",
            session_name="alpha-navigator",
        )
        is False
    )


async def test_a_turn_that_fails_lets_the_compaction_go_ahead(tmp_path: Path) -> None:
    """The compaction is waiting on this. Every failure has to end in the
    summary simply proceeding."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))

    class Broken:
        async def ask(self, text: str, **_):
            raise RuntimeError("the CLI died")

    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": Broken()},
    )

    assert (
        await recorder.write(
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
            agent_id="claude-code",
            session_name="alpha-navigator",
        )
        is False
    )
    assert await recorder.take("9f1c2b3a-0000-0000-0000-000000000000") is None


async def test_a_record_that_runs_long_is_given_up_on(tmp_path: Path) -> None:
    """`PreCompact` is holding the session. The wait is bounded, and past it
    the summary proceeds without a record."""
    import halyard.core.compaction as compaction

    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("late", delay=5)},
    )
    original = compaction.RECORD_TIMEOUT_SECONDS
    compaction.RECORD_TIMEOUT_SECONDS = 0.05
    try:
        written = await recorder.write(
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
            agent_id="claude-code",
            session_name="alpha-navigator",
        )
    finally:
        compaction.RECORD_TIMEOUT_SECONDS = original

    assert written is False
    assert await recorder.take("9f1c2b3a-0000-0000-0000-000000000000") is None


# --- saying that it is happening ---------------------------------------------


class Chatty:
    """A channel that records what it was asked to say."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def send_message(self, session_id, text, role=None, **_) -> str:
        self.said.append(text)
        return "ok"


async def test_the_seat_is_told_a_compaction_started_and_finished(tmp_path: Path) -> None:
    """Found in the field: the first compaction arrived as a session that had
    simply stopped answering, and working out why meant walking to the desk and
    then reading a log. Both moments are known exactly."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    channel = Chatty()
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("the record")},
        channel=channel,
    )

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )
    await recorder.take(
        "9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert "compacting" in channel.said[0]
    assert "finished compacting" in channel.said[1]
    # The numbers, because the question after "what happened" is "how long".
    assert "characters across" in channel.said[1]


async def test_a_seat_with_nothing_configured_is_still_told(tmp_path: Path) -> None:
    """What confused somebody was the pause, not the record — so this is said
    for every seat rather than only the ones carrying a file across."""
    channel = Chatty()
    recorder = Recorder(roots=(tmp_path,), seats=[seat()], runners={}, channel=channel)

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )
    await recorder.take(
        "9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert len(channel.said) == 2


async def test_a_paused_gate_says_nothing(tmp_path: Path) -> None:
    """Paused means the phone is off, which is how the reply relay and the
    transcript watcher both read it."""
    from halyard.core.gate import Gate

    gate = Gate()
    await gate.pause("tester")
    channel = Chatty()
    recorder = Recorder(roots=(tmp_path,), seats=[seat()], runners={}, channel=channel, gate=gate)

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    assert channel.said == []


async def test_a_channel_that_fails_does_not_reach_the_session(tmp_path: Path) -> None:
    """The compaction is waiting on this hook. An undelivered notification must
    not be why a session does not come back."""

    class Broken:
        async def send_message(self, *a, **k):
            raise ConnectionError("telegram unreachable")

    recorder = Recorder(roots=(tmp_path,), seats=[seat()], runners={}, channel=Broken())

    assert (
        await recorder.write(
            session_id="9f1c2b3a-0000-0000-0000-000000000000",
            agent_id="claude-code",
            session_name="alpha-navigator",
        )
        is False
    )


async def test_a_session_with_no_seat_is_not_announced(tmp_path: Path) -> None:
    """Nowhere to say it, and guessing a chat would put a message in front of
    somebody who is not running this."""
    channel = Chatty()
    recorder = Recorder(roots=(tmp_path,), seats=[], runners={}, channel=channel)

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="nobody",
    )

    assert channel.said == []


async def test_the_record_is_asked_for_what_is_in_flight_first(tmp_path: Path) -> None:
    """A navigator reported it after coming out of one: the summary keeps
    history well and loses work in flight — a report received and not checked,
    a command run whose output was never read."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("their own instructions", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    await recorder.write(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    asked = runner.asked[0]
    assert "IN FLIGHT" in asked
    # And facts rather than errands: the reading list is what lost to the
    # operator's next message.
    assert "not errands" in asked
    # The seat's own file still leads; the wrapper only says how to write it.
    assert asked.index("their own instructions") < asked.index("IN FLIGHT")


async def test_two_overlapping_compactions_each_get_their_own_record(tmp_path: Path) -> None:
    """Measured in the field, on one session in six minutes:

        14:41:37 before   14:43:11 before
        14:45:46 after    14:47:32 after

    With one slot per session the second record overwrote the first, the first
    `after` collected the second compaction's record, and the second `after`
    got nothing at all.
    """
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    runner = FakeRunner("first")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )
    session = dict(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    await recorder.write(**session)
    runner.answer = "second"
    await recorder.write(**session)

    assert await recorder.take(**session) == "first"
    assert await recorder.take(**session) == "second"
    assert await recorder.take(**session) is None


async def test_records_nobody_collects_do_not_pile_up(tmp_path: Path) -> None:
    """Past a few, nothing is collecting them, and holding more would keep text
    nobody will ever be handed. The oldest goes, being the most out of date."""
    import halyard.core.compaction as compaction

    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    transcript(tmp_path / "9f1c2b3a-0000-0000-0000-000000000000.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )
    session = dict(
        session_id="9f1c2b3a-0000-0000-0000-000000000000",
        agent_id="claude-code",
        session_name="alpha-navigator",
    )

    for index in range(compaction.MAX_WAITING + 2):
        runner.answer = f"record-{index}"
        await recorder.write(**session)

    kept = [await recorder.take(**session) for _ in range(compaction.MAX_WAITING)]

    assert kept == [f"record-{i}" for i in range(2, compaction.MAX_WAITING + 2)]
    assert await recorder.take(**session) is None
