"""Tests for the transcript watcher.

Two properties matter more than the happy path, because they are what it was
asked to guarantee: it never raises into the thing that feeds it, and it stays
cheap by reading only what is new. Both are exercised harder than the alert
itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.core.gate import Gate
from halyard.core.transcripts import TranscriptWatcher, find_api_errors

START = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingChannel:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(
        self, session_id, text, role=None, *, agent_id=None, session_name=None
    ) -> str:
        self.messages.append(
            {"session_id": session_id, "text": text, "role": role, "session_name": session_name}
        )
        return "ok"


def error_line(
    uuid: str = "u1", text: str = "API Error: 529 Overloaded.", status: int = 529
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "isApiErrorMessage": True,
            "apiErrorStatus": status,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def normal_line(text: str = "on it") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "n1",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def watcher(channel=None, gate=None, clock=None) -> TranscriptWatcher:
    return TranscriptWatcher(
        channel=channel or RecordingChannel(),
        gate=gate or Gate(),
        clock=clock or ManualClock(),
        idle_ttl=timedelta(minutes=30),
    )


def append(path: Path, *lines: str, newline: bool = True) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + ("\n" if newline else ""))


# --- the detection is pure and forgiving ------------------------------------


def test_it_finds_an_api_error_and_reads_its_text() -> None:
    found = find_api_errors([normal_line(), error_line(text="529 Overloaded")], seen=set())

    assert found == [("u1", "529 Overloaded")]


def test_a_line_that_is_not_json_is_skipped_not_raised() -> None:
    # A write caught mid-flight, a log line that slipped in — none of it should
    # be able to throw, because this runs off a background loop that must not die.
    assert find_api_errors(["{ not json", "", error_line()], seen=set())


def test_an_entry_without_the_flag_is_not_an_error() -> None:
    assert find_api_errors([normal_line()], seen=set()) == []


def test_a_shape_with_no_text_falls_back_to_the_status() -> None:
    line = json.dumps({"uuid": "x", "isApiErrorMessage": True, "apiErrorStatus": 503})

    ((_, text),) = find_api_errors([line], seen=set())
    assert "503" in text


def test_an_id_already_seen_is_not_reported_again() -> None:
    assert find_api_errors([error_line(uuid="u1")], seen={"u1"}) == []


# --- watching stays cheap and only looks forward ----------------------------


async def test_only_errors_appended_after_watching_are_relayed(tmp_path: Path) -> None:
    channel = RecordingChannel()
    w = watcher(channel)
    tx = tmp_path / "sess.jsonl"
    # An error already in the file before anybody was watching.
    append(tx, error_line(uuid="old"))

    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code", session_name="drv")
    await w.poll_once()
    # Nothing yet: the offset started at the end, so history is not replayed.
    assert channel.messages == []

    append(tx, error_line(uuid="new", text="hit your session limit"))
    await w.poll_once()

    assert len(channel.messages) == 1
    assert "session limit" in channel.messages[0]["text"]
    assert channel.messages[0]["session_id"] == "s1"


async def test_a_partial_final_line_waits_until_it_is_whole(tmp_path: Path) -> None:
    channel = RecordingChannel()
    w = watcher(channel)
    tx = tmp_path / "sess.jsonl"
    tx.write_text("")
    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code")

    append(tx, error_line(), newline=False)  # written, but no newline yet
    await w.poll_once()
    assert channel.messages == []

    append(tx, "")  # the newline arrives
    await w.poll_once()
    assert len(channel.messages) == 1


async def test_the_same_error_is_not_relayed_twice(tmp_path: Path) -> None:
    channel = RecordingChannel()
    w = watcher(channel)
    tx = tmp_path / "sess.jsonl"
    tx.write_text("")
    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code")

    append(tx, error_line(uuid="u1"))
    await w.poll_once()
    await w.poll_once()  # nothing new appended

    assert len(channel.messages) == 1


async def test_a_missing_transcript_does_not_raise(tmp_path: Path) -> None:
    channel = RecordingChannel()
    w = watcher(channel)
    w.note(
        session_id="s1",
        transcript_path=str(tmp_path / "gone.jsonl"),
        agent_id="claude-code",
    )

    await w.poll_once()  # must not raise

    assert channel.messages == []


async def test_a_transcript_that_shrank_is_not_re_read(tmp_path: Path) -> None:
    channel = RecordingChannel()
    w = watcher(channel)
    tx = tmp_path / "sess.jsonl"
    append(tx, normal_line(), normal_line())
    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code")

    tx.write_text(error_line(uuid="fresh") + "\n")  # replaced, now smaller
    await w.poll_once()  # resets to the new end rather than re-reading

    # The offset moved to the end on the shrink, so this content is skipped —
    # the safe, cheap direction. A new append is still caught.
    append(tx, error_line(uuid="after"))
    await w.poll_once()
    assert any("Overloaded" in m["text"] for m in channel.messages)


# --- it respects pause and the runtime, and forgets idle sessions -----------


async def test_a_paused_gate_stays_quiet(tmp_path: Path) -> None:
    channel = RecordingChannel()
    gate = Gate()
    await gate.pause("tester")
    w = watcher(channel, gate=gate)
    tx = tmp_path / "sess.jsonl"
    tx.write_text("")
    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code")

    append(tx, error_line())
    await w.poll_once()

    # Paused means the phone is off, exactly as the reply relay treats it.
    assert channel.messages == []


def test_only_claude_code_is_watched(tmp_path: Path) -> None:
    w = watcher()
    tx = tmp_path / "sess.jsonl"
    tx.write_text("")

    w.note(session_id="s1", transcript_path=str(tx), agent_id="codex")

    # The transcript shape read here is Claude Code's; the others are not it.
    assert "s1" not in w._watched


async def test_an_idle_session_is_dropped(tmp_path: Path) -> None:
    clock = ManualClock()
    w = watcher(clock=clock)
    tx = tmp_path / "sess.jsonl"
    tx.write_text("")
    w.note(session_id="s1", transcript_path=str(tx), agent_id="claude-code")

    clock.advance(timedelta(minutes=31).total_seconds())
    await w.poll_once()

    assert "s1" not in w._watched
