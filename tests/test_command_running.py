"""Tests for `halyard.commands.running` — a project command, run and told."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from halyard.commands import Result, outcome, run, summary


def test_a_command_says_it_started_before_anything_else(tmp_path: Path, caplog) -> None:
    """So a run that never reports back has still left a line saying it began,
    whoever asked for it — `/command`, a commit's check or a transition."""
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
        "Ran test-fast: make test-fast · exit 0 (passed) · 94s · last line: 1420 passed"
    )
    assert summary("test-fast", "make test-fast", failed).endswith(
        "exit 1 (failed) · 31s · last line: 2 failed · log: build/tests.log"
    )


def test_one_line_for_a_run_that_did_not_finish() -> None:
    stopped = Result(
        ok=False, output="It was still running after 600s.", seconds=600, timed_out=True
    )
    refused = Result(ok=False, output="No such file or directory", seconds=0.0)

    assert summary("test-fast", "make test-fast", stopped).endswith(
        "· stopped after 600s (not run)"
    )
    assert summary("test-fast", "make test-fast", refused).endswith(
        "· could not start: No such file or directory (not run)"
    )


def ended(code: int | None, *, timed_out: bool = False) -> Result:
    return Result(ok=code == 0, output="", seconds=1.0, exit_code=code, timed_out=timed_out)


@pytest.mark.parametrize(
    ("result", "said"),
    [
        (ended(0), "passed"),
        (ended(1), "failed"),
        (ended(2), "failed"),
        (ended(127), "not run"),
        (ended(126), "not run"),
        (ended(130), "not run"),
        (ended(-9), "not run"),
        (ended(None), "not run"),
        (ended(None, timed_out=True), "not run"),
    ],
)
def test_how_a_command_ended_needs_nothing_declared(result: Result, said: str) -> None:
    """Not found, not executable, ended by a signal, out of time, never
    started: no result at all, for any command."""
    assert outcome(result) == said


def test_a_command_says_which_of_its_own_codes_mean_it_did_not_run() -> None:
    """Only it can know its 2 means "nothing here to run" and not "red"."""
    assert outcome(ended(2), (2,)) == "not run"
    assert outcome(ended(1), (2,)) == "failed"
    assert summary("test-fast", "scripts/check.sh", ended(2), (2,)).startswith(
        "Ran test-fast: scripts/check.sh · exit 2 (not run)"
    )
