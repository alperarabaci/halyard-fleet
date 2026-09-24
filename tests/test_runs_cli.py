"""Tests for `halyard runs` — the workflow runs kept in the database, read back
without SQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from halyard import runs_cli
from halyard.workflows import Round, Run
from halyard.workflows.journal import record

AT = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)

#: A review sent back once and passed the second time, the navigator acting on
#: the reviewer's word both times.
REVIEWED = Run(
    workflow="level3",
    step=1,
    since=AT,
    chat="-100777",
    rounds={
        "review": (
            Round(at=AT, to="xrev", decision="back", decided_by="review"),
            Round(
                at=AT + timedelta(minutes=20), to="xrev", decision="forward", decided_by="review"
            ),
        ),
        "reviewed": (
            Round(at=AT + timedelta(minutes=5), to="nav", decision="back", decided_by="review"),
            Round(at=AT + timedelta(minutes=30), to="nav", decision="forward", decided_by="review"),
        ),
    },
)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A database of its own, never the one the running service keeps."""
    path = tmp_path / "halyard.db"
    monkeypatch.setattr(runs_cli, "_database", lambda: path)
    return path


def kept(database: Path, run: Run = REVIEWED, work: str = "alpha-engine#386") -> None:
    record(
        database,
        run,
        project="alpha-engine",
        work=work,
        outcome="done",
        finished=AT + timedelta(minutes=35),
    )


def test_plain_runs_lists_the_latest_a_line_each(database: Path, capsys) -> None:
    kept(database)

    assert runs_cli.main([]) == 0

    [line] = capsys.readouterr().out.splitlines()
    assert line.split()[2:] == ["alpha-engine#386", "level3", "done", "35", "min", "4", "steps"]


def test_a_work_is_shown_step_by_step_with_what_each_answer_decided(database: Path, capsys) -> None:
    kept(database)

    assert runs_cli.main(["#386"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "alpha-engine#386 · level3 · done"
    assert " → " in lines[1] and lines[1].endswith("35 min")
    assert lines[3].split() == ["at", "step", "round", "agent", "decided"]
    assert [line.split()[1:] for line in lines[4:]] == [
        ["review", "1", "xrev", "back"],
        ["reviewed", "1", "nav", "back", "(review)"],
        ["review", "2", "xrev", "forward"],
        ["reviewed", "2", "nav", "forward", "(review)"],
    ]


def test_a_run_with_phases_says_each_step_s_phase(database: Path, capsys) -> None:
    phased = Run(
        workflow="level3phased",
        step=2,
        since=AT,
        chat="-100777",
        rounds={
            "discover@1": (Round(at=AT, to="xdrv", decision="forward", decided_by="discover"),),
            "develop@2": (Round(at=AT + timedelta(minutes=9), to="xdrv"),),
        },
    )
    kept(database, phased)

    assert runs_cli.main(["alpha-engine#386"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[3].split() == ["at", "step", "phase", "round", "agent", "decided"]
    assert lines[4].split()[1:] == ["discover", "1", "1", "xdrv", "forward"]
    assert lines[5].split()[1:] == ["develop", "2", "1", "xdrv"], "an answer that never came"


def test_a_work_that_had_no_run_says_so(database: Path, capsys) -> None:
    kept(database)

    assert runs_cli.main(["#999"]) == 1
    assert "no run of '#999'" in capsys.readouterr().err


def test_nothing_kept_yet_says_so(database: Path, capsys) -> None:
    assert runs_cli.main(["recent"]) == 0
    assert "No workflow run is kept" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["recent", "0"], ["recent", "ten"], ["#386", "#387"], ["--all"]])
def test_what_it_does_not_take_is_refused_with_how_to_ask(
    database: Path, capsys, args: list[str]
) -> None:
    assert runs_cli.main(args) == 2
    assert "usage: halyard runs" in capsys.readouterr().err
