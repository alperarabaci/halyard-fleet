"""Tests for `halyard runs` — the workflow runs kept in the database, read back
without SQL."""

from __future__ import annotations

from dataclasses import replace
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


# --- a run by its own name, and one still going --------------------------------


def going(database: Path, run: Run, work: str = "alpha-engine#453") -> None:
    """A run not finished yet, in its project's file beside the database."""
    from halyard.workflows import save
    from halyard.workflows.runs import FILE

    save(database.parent / "projects" / "alpha-engine" / FILE, work, run)


def test_a_run_still_going_is_shown_by_its_work(database: Path, capsys) -> None:
    """It is only kept in the database when it ends, and the run worth looking
    at is usually the one that has not."""
    waiting = replace(REVIEWED, ident="3f2a9c1e", waiting_for="xnav", waiting=True)
    going(database, waiting)

    code = runs_cli.main(["#453"])

    out = capsys.readouterr().out
    assert code == 0
    assert "alpha-engine#453 · level3 · waiting for xnav" in out
    assert "run 3f2a9c1e" in out
    assert "review" in out and "reviewed" in out


def test_a_run_is_found_by_its_own_name(database: Path, capsys) -> None:
    going(
        database,
        replace(REVIEWED, ident="3f2a9c1e", waiting=False, stopped="it was asked to wait"),
    )
    kept(database, replace(REVIEWED, ident="aa11bb22"), work="alpha-engine#400")

    assert runs_cli.main(["3f2a9c1e"]) == 0
    first = capsys.readouterr().out
    assert runs_cli.main(["aa11bb22"]) == 0
    second = capsys.readouterr().out

    assert "alpha-engine#453 · level3 · stopped: it was asked to wait" in first
    assert "alpha-engine#400 · level3 · done" in second and "run aa11bb22" in second


def test_a_name_nothing_has_is_said(database: Path, capsys) -> None:
    assert runs_cli.main(["ffffffff"]) == 1
    assert "or still going" in capsys.readouterr().err
