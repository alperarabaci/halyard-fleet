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
    path = transcript(tmp_path / "t.jsonl", ("user", "run the tests"), ("assistant", "712 passed"))

    text = conversation_tail(path, roots=(tmp_path,))

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

    assert "ok" in conversation_tail(path, roots=(tmp_path,))


def test_a_missing_transcript_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    assert conversation_tail(tmp_path / "gone.jsonl", roots=(tmp_path,)) == ""


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

    assert conversation_tail(path, roots=(tmp_path,)) == ""


# --- what a seat is handed afterwards ----------------------------------------


def test_a_seat_with_a_file_gets_its_contents(tmp_path: Path) -> None:
    orientation = tmp_path / "post.md"
    orientation.write_text("read the queue first", encoding="utf-8")
    seats = [seat(after_compaction=str(orientation))]

    found = for_seat(seats, agent_id="claude-code", session_name="alpha-navigator", session_id="s1")

    assert found == "read the queue first"


def test_a_seat_without_one_is_handed_nothing(tmp_path: Path) -> None:
    """The common case. A driver running one command has no use for a page."""
    assert (
        for_seat([seat()], agent_id="claude-code", session_name="alpha-navigator", session_id="s1")
        is None
    )


def test_a_file_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    seats = [seat(after_compaction=str(tmp_path / "missing.md"))]

    assert (
        for_seat(seats, agent_id="claude-code", session_name="alpha-navigator", session_id="s1")
        is None
    )


def test_an_unresolved_session_is_handed_nothing(tmp_path: Path) -> None:
    """A seat is `(runtime, session)`. Handing a Codex session the Claude
    navigator's orientation would be worse than handing it none."""
    orientation = tmp_path / "post.md"
    orientation.write_text("claude only", encoding="utf-8")
    seats = [seat(after_compaction=str(orientation))]

    assert (
        for_seat(seats, agent_id="codex", session_name="alpha-navigator", session_id="s1") is None
    )


def test_an_enormous_file_is_bounded(tmp_path: Path) -> None:
    big = tmp_path / "post.md"
    big.write_text("x" * 100_000, encoding="utf-8")

    assert len(read(big) or "") <= 32_000


# --- writing the record -------------------------------------------------------


async def test_the_record_is_written_and_handed_back(tmp_path: Path) -> None:
    instructions = tmp_path / "pre.md"
    instructions.write_text("write down the measured numbers", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "712 passed, I ran it"))
    runner = FakeRunner("AKTIF IS: 712 passed — measured")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    written = await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path=str(path),
    )

    assert written
    assert recorder.take("s1") == "AKTIF IS: 712 passed — measured"
    # The instructions and the conversation both reached the one-shot turn.
    assert "write down the measured numbers" in runner.asked[0]
    assert "712 passed" in runner.asked[0]


async def test_the_record_is_written_by_the_cheap_model(tmp_path: Path) -> None:
    """A distillation of text somebody else already wrote. The reasoning
    happened in the session, not here."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path=str(path),
    )

    assert runner.models == ["sonnet"]


async def test_a_long_record_is_trimmed_before_it_is_carried(tmp_path: Path) -> None:
    """It goes into a context that was just emptied on purpose. Carrying a long
    one across would refill what the compaction had cleared."""
    import halyard.core.compaction as compaction

    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("y" * 50_000)},
    )

    await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path=str(path),
    )

    assert len(recorder.take("s1") or "") <= compaction.RECORD_LIMIT


async def test_the_prompt_asks_for_brevity_as_well(tmp_path: Path) -> None:
    """A model told to be brief writes something better than one whose answer
    is cut off, so the limit is asked for and enforced."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))
    runner = FakeRunner("x")
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": runner},
    )

    await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path=str(path),
    )

    assert "characters" in runner.asked[0]


async def test_the_record_is_handed_over_once(tmp_path: Path) -> None:
    """It describes one compaction. Leaving it behind would hand it to the next
    one as though it were fresh."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("once")},
    )
    await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path=str(path),
    )

    assert recorder.take("s1") == "once"
    assert recorder.take("s1") is None


async def test_a_seat_without_instructions_writes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    recorder = Recorder(roots=(tmp_path,), seats=[seat()], runners={"claude-code": runner})

    written = await recorder.write(
        session_id="s1",
        agent_id="claude-code",
        session_name="alpha-navigator",
        transcript_path="/x",
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
            session_id="s1",
            agent_id="claude-code",
            session_name="alpha-navigator",
            transcript_path="/x",
        )
        is False
    )


async def test_a_turn_that_fails_lets_the_compaction_go_ahead(tmp_path: Path) -> None:
    """The compaction is waiting on this. Every failure has to end in the
    summary simply proceeding."""
    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))

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
            session_id="s1",
            agent_id="claude-code",
            session_name="alpha-navigator",
            transcript_path=str(path),
        )
        is False
    )
    assert recorder.take("s1") is None


async def test_a_record_that_runs_long_is_given_up_on(tmp_path: Path) -> None:
    """`PreCompact` is holding the session. The wait is bounded, and past it
    the summary proceeds without a record."""
    import halyard.core.compaction as compaction

    instructions = tmp_path / "pre.md"
    instructions.write_text("record", encoding="utf-8")
    path = transcript(tmp_path / "t.jsonl", ("assistant", "done"))
    recorder = Recorder(
        roots=(tmp_path,),
        seats=[seat(before_compaction=str(instructions))],
        runners={"claude-code": FakeRunner("late", delay=5)},
    )
    original = compaction.RECORD_TIMEOUT_SECONDS
    compaction.RECORD_TIMEOUT_SECONDS = 0.05
    try:
        written = await recorder.write(
            session_id="s1",
            agent_id="claude-code",
            session_name="alpha-navigator",
            transcript_path=str(path),
        )
    finally:
        compaction.RECORD_TIMEOUT_SECONDS = original

    assert written is False
    assert recorder.take("s1") is None
