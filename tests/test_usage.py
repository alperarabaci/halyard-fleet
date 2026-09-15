"""Tests for `halyard.core.usage` — what the turns Halyard starts itself use —
and for the rows a Claude Code answer becomes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.agents.claude_code.runner import turns_used
from halyard.core import usage

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def answer(session: str = "s-1") -> dict:
    """What Claude Code 2.1.270 answered for a one-word sonnet turn, measured on
    2026-09-15 — cut down to the fields read here."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": session,
        "total_cost_usd": 0.184456,
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 46103,
            "cache_read_input_tokens": 0,
            "output_tokens": 4,
        },
        "modelUsage": {
            "claude-sonnet-5": {
                "inputTokens": 2,
                "outputTokens": 4,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 46103,
                "costUSD": 0.184456,
            }
        },
    }


def test_one_row_per_model_with_the_cache_kept_apart_from_the_question() -> None:
    """The measured turn: two tokens of question, 46,103 written to the cache."""
    [turn] = turns_used(answer(), purpose="check claims", project="alpha-engine")

    assert turn.model == "claude-sonnet-5"
    assert (turn.input_tokens, turn.output_tokens) == (2, 4)
    assert (turn.cache_write_tokens, turn.cache_read_tokens) == (46103, 0)
    assert turn.cost_usd == 0.184456
    assert (turn.purpose, turn.project, turn.session_id) == ("check claims", "alpha-engine", "s-1")


def test_without_a_breakdown_the_turns_own_usage_is_the_row() -> None:
    without = {key: value for key, value in answer().items() if key != "modelUsage"}

    [turn] = turns_used(without)

    assert turn.model is None
    assert turn.cache_write_tokens == 46103
    assert turn.cost_usd == 0.184456


def test_an_answer_that_says_nothing_of_usage_records_nothing() -> None:
    assert turns_used({"result": "ok"}) == []


def test_what_was_recorded_is_read_back_by_model_and_purpose(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    turns = [("a", "check claims"), ("b", "check claims"), ("c", "commit message")]
    for session, purpose in turns:
        usage.record(database, turns_used(answer(session), purpose=purpose), at=NOW)
    old = turns_used(answer("d"), purpose="check proof")
    usage.record(database, old, at=NOW - timedelta(days=8))

    rows = {purpose: rest for _, purpose, *rest in usage.totals(database, NOW - timedelta(days=7))}
    said = usage.report(database, now=NOW)

    assert set(rows) == {"check claims", "commit message"}
    assert rows["check claims"][0] == 2
    assert rows["check claims"][3] == 92206
    assert "check claims" in said and "check proof" not in said
    assert "92,206" in said


def test_a_record_that_cannot_be_kept_never_fails_the_turn(tmp_path: Path) -> None:
    """A directory where the file should be: the row is lost, and that is all."""
    usage.record(tmp_path, turns_used(answer()), at=NOW)


def test_nothing_recorded_says_so(tmp_path: Path) -> None:
    assert "Nothing recorded" in usage.report(tmp_path / "halyard.db", now=NOW)


def test_a_number_of_days_that_is_not_one_is_refused(capsys) -> None:
    assert usage.main(["a week"]) == 2
    assert "not a number of days" in capsys.readouterr().err
