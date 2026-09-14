"""Tests for `halyard.commands.running` — a project command, run and told."""

from __future__ import annotations

import logging
from pathlib import Path

from halyard.commands import Result, run, summary


def test_a_command_says_it_started_before_anything_else(tmp_path: Path, caplog) -> None:
    """So a run that never reports back has still left a line saying it began,
    whoever asked for it — `/command`, a commit's check or a handoff."""
    caplog.set_level(logging.INFO)

    result = run("echo hi", tmp_path)

    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == f"Command started in {tmp_path}: echo hi"
    assert result.ok
    assert result.exit_code == 0


def test_a_failing_command_keeps_its_exit_code(tmp_path: Path) -> None:
    result = run("echo broke; exit 3", tmp_path)

    assert not result.ok
    assert result.exit_code == 3
    assert result.output == "broke"


def test_a_command_out_of_time_says_so_in_the_log(tmp_path: Path, caplog) -> None:
    result = run("sleep 5", tmp_path, timeout=0.3)

    assert result.timed_out
    assert result.exit_code is None
    assert any("Command stopped after" in record.getMessage() for record in caplog.records)


def test_one_line_says_how_it_ended_and_the_last_thing_it_printed() -> None:
    """The last line is where a test runner puts its count, and where a
    project's own target puts its status and the file the run went to."""
    passed = Result(ok=True, output="collected\n1420 passed", seconds=94.2, exit_code=0)
    failed = Result(ok=False, output="2 failed · log: build/tests.log", seconds=31, exit_code=1)

    assert summary("test-fast", "make test-fast", passed) == (
        "Ran test-fast: make test-fast · exit 0 · 94s · last line: 1420 passed"
    )
    assert summary("test-fast", "make test-fast", failed).endswith(
        "exit 1 · 31s · last line: 2 failed · log: build/tests.log"
    )


def test_one_line_for_a_run_that_did_not_finish() -> None:
    stopped = Result(
        ok=False, output="It was still running after 600s.", seconds=600, timed_out=True
    )
    refused = Result(ok=False, output="No such file or directory", seconds=0.0)

    assert summary("test-fast", "make test-fast", stopped).endswith("· stopped after 600s")
    assert summary("test-fast", "make test-fast", refused).endswith(
        "· could not start: No such file or directory"
    )
