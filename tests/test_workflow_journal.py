"""Tests for `halyard.workflows.journal` — what a finished run did, kept in the
database beside the tokens, and said in a line or two."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.workflows import Round, Run
from halyard.workflows.journal import deliveries, lasted, record, run_id, steps_line

AT = datetime(2026, 9, 23, 11, 2, tzinfo=UTC)
FLOW = ("to_nav", "review", "reviewed", "discover", "develop", "developed", "close")
STRETCH = (3, 5)


def went(*steps: tuple[str, str, int]) -> Run:
    """A run whose steps reached their seats, as `(key, seat, minutes after start)`."""
    rounds: dict[str, tuple[Round, ...]] = {}
    for key, seat, minutes in steps:
        rounds[key] = (*rounds.get(key, ()), Round(at=AT + timedelta(minutes=minutes), to=seat))
    return Run(workflow="level3phased", step=6, since=AT, chat="-100777", rounds=rounds)


RUN = went(
    ("to_nav", "nav", 0),
    ("review", "xreview", 5),
    ("reviewed", "nav", 20),
    ("review", "xreview", 30),
    ("reviewed", "nav", 45),
    ("discover@1", "xdrv", 50),
    ("develop@1", "xdrv", 80),
    ("developed@1", "nav", 120),
    ("develop@2", "xdrv", 130),
    ("developed@2", "nav", 200),
    ("close", "nav", 218),
)


def test_the_steps_are_said_once_each_with_their_rounds_and_phases() -> None:
    assert steps_line(RUN, flow=FLOW, stretch=STRETCH) == (
        "to_nav · review x2 · reviewed x2 · phase 1: discover, develop, developed · "
        "phase 2: develop, developed · close"
    )


def test_a_flow_without_phases_says_its_steps_in_order() -> None:
    run = went(("review", "xreview", 0), ("to_nav", "nav", 5), ("review", "xreview", 9))

    assert steps_line(run, flow=("review", "to_nav"), stretch=None) == "review x2 · to_nav"


def test_a_run_that_reached_nobody_says_so() -> None:
    assert steps_line(went(), flow=FLOW, stretch=STRETCH) == "no step reached its agent"


def test_how_long_is_said_the_way_somebody_would() -> None:
    assert lasted(AT, AT + timedelta(minutes=38)) == "38 min"
    assert lasted(AT, AT + timedelta(minutes=218)) == "3 h 38 min"
    assert lasted(AT, AT + timedelta(hours=3)) == "3 h"
    assert lasted(AT, AT + timedelta(hours=52)) == "2 d 4 h"
    assert lasted(AT, AT + timedelta(days=2)) == "2 d"


def test_every_delivery_is_kept_in_the_order_it_happened() -> None:
    kept = deliveries(RUN)

    assert [(step, phase, number) for _, step, phase, number, _ in kept[:6]] == [
        ("to_nav", None, 1),
        ("review", None, 1),
        ("reviewed", None, 1),
        ("review", None, 2),
        ("reviewed", None, 2),
        ("discover", 1, 1),
    ]
    assert [at for at, *_ in kept] == sorted(at for at, *_ in kept)


def test_a_finished_run_is_kept_beside_the_tokens(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    finished = AT + timedelta(minutes=220)

    record(
        database,
        RUN,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="done",
        finished=finished,
    )

    with sqlite3.connect(database) as db:
        [row] = db.execute(
            "SELECT project, work, workflow, outcome, phases, deliveries FROM workflow_runs"
        ).fetchall()
        steps = db.execute(
            "SELECT step, phase, round, agent FROM workflow_steps WHERE run_id = ? ORDER BY at",
            (run_id(RUN, "alpha-engine#386"),),
        ).fetchall()
    assert row == ("alpha-engine", "alpha-engine#386", "level3phased", "done", 2, 11)
    assert steps[0] == ("to_nav", None, 1, "nav")
    assert steps[-1] == ("close", None, 1, "nav")
    assert ("develop", 2, 1, "xdrv") in steps


def test_steps_kept_while_agents_were_seats_keep_their_rows(tmp_path: Path) -> None:
    """`workflow_steps.seat` until 2026-09-24. A machine that kept runs then has
    the column renamed on the next run it keeps, and the old rows with it."""
    import contextlib

    from halyard.workflows import journal

    database = tmp_path / "halyard.db"
    before = journal._SCHEMA.replace("    agent  TEXT NOT NULL", "    seat   TEXT NOT NULL")
    assert "agent" not in before, "the table as it was, or this proves nothing"
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(before)
        db.execute(
            "INSERT INTO workflow_steps VALUES "
            "('an older run', '2026-09-23T09:00:00+00:00', 'review', NULL, 1, 'xreview')"
        )
        db.commit()

    record(
        database,
        RUN,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="done",
        finished=AT + timedelta(minutes=220),
    )

    with contextlib.closing(sqlite3.connect(database)) as db:
        kept = db.execute("SELECT run_id, agent FROM workflow_steps ORDER BY at").fetchall()
    assert kept[0] == ("an older run", "xreview")
    assert len(kept) == 12


def test_keeping_the_same_run_twice_keeps_it_once(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    for outcome in ("stopped", "done"):
        record(
            database,
            RUN,
            project="alpha-engine",
            work="alpha-engine#386",
            outcome=outcome,
            finished=AT,
        )

    with sqlite3.connect(database) as db:
        runs = db.execute("SELECT outcome FROM workflow_runs").fetchall()
        steps = db.execute("SELECT COUNT(*) FROM workflow_steps").fetchone()
    assert runs == [("done",)]
    assert steps == (11,)


def test_a_record_that_cannot_be_kept_costs_nothing_else(tmp_path: Path) -> None:
    """A directory where the database should be: the run still ends."""
    record(
        tmp_path,
        RUN,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="done",
        finished=AT,
    )
