"""Tests for `halyard.workflows.journal` — what a finished run did, kept in the
database beside the tokens, and said in a line or two."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.workflows import Round, Run
from halyard.workflows.journal import (
    deliveries,
    lasted,
    of_work,
    recent,
    record,
    run_id,
    steps_line,
    steps_of,
)

AT = datetime(2026, 9, 23, 11, 2, tzinfo=UTC)

#: `workflow_steps` as 2026-09-23 wrote it: the agent a seat, and no decisions.
SEATED = """
CREATE TABLE workflow_steps (
    run_id TEXT NOT NULL,
    at     TEXT NOT NULL,
    step   TEXT NOT NULL,
    phase  INTEGER,
    round  INTEGER NOT NULL,
    seat   TEXT NOT NULL
);
"""

#: As 2026-09-24 wrote it: the agent named, and still no decisions.
UNDECIDED = SEATED.replace("    seat   TEXT NOT NULL", "    agent  TEXT NOT NULL")
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

    assert [(step.step, step.phase, step.round) for step in kept[:6]] == [
        ("to_nav", None, 1),
        ("review", None, 1),
        ("reviewed", None, 1),
        ("review", None, 2),
        ("reviewed", None, 2),
        ("discover", 1, 1),
    ]
    assert [step.at for step in kept] == sorted(step.at for step in kept)


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
    database = tmp_path / "halyard.db"
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(SEATED)
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


#: A review sent back once: the reviewer's word, and the navigator acting on it.
DECIDED = Run(
    workflow="level3",
    step=1,
    since=AT,
    chat="-100777",
    rounds={
        "review": (
            Round(at=AT, to="xrev", decision="back", decided_by="review"),
            Round(at=AT + timedelta(minutes=20), to="xrev"),
        ),
        "reviewed": (
            Round(at=AT + timedelta(minutes=5), to="nav", decision="back", decided_by="review"),
        ),
    },
)


def test_each_step_keeps_what_its_answer_decided_and_on_whose_word(tmp_path: Path) -> None:
    """How the run moved, not only where it went. A round nobody answered
    decided nothing, and says so as nothing."""
    database = tmp_path / "halyard.db"

    record(
        database,
        DECIDED,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="stopped",
        finished=AT,
    )

    with contextlib.closing(sqlite3.connect(database)) as db:
        steps = db.execute(
            "SELECT step, round, decision, decided_by FROM workflow_steps ORDER BY at"
        ).fetchall()
    assert steps == [
        ("review", 1, "back", "review"),
        ("reviewed", 1, "back", "review"),
        ("review", 2, None, None),
    ]


def test_steps_kept_before_decisions_keep_their_rows(tmp_path: Path) -> None:
    """A machine that kept runs before 2026-09-25 gains the two columns on the
    next run it keeps, and its old rows read as having decided nothing."""
    database = tmp_path / "halyard.db"
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(UNDECIDED)
        db.execute(
            "INSERT INTO workflow_steps VALUES "
            "('an older run', '2026-09-22T09:00:00+00:00', 'review', NULL, 1, 'xreview')"
        )
        db.commit()

    record(
        database,
        DECIDED,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="stopped",
        finished=AT,
    )

    with contextlib.closing(sqlite3.connect(database)) as db:
        kept = db.execute(
            "SELECT run_id, agent, decision FROM workflow_steps ORDER BY at"
        ).fetchall()
    assert kept[0] == ("an older run", "xreview", None)
    assert kept[1][2] == "back"


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


# --- reading them back ---------------------------------------------------------


def kept_runs(database: Path) -> None:
    """Two pieces of work, the second started an hour after the first."""
    for number, hours in ((386, 0), (387, 1)):
        record(
            database,
            replace(RUN, since=AT + timedelta(hours=hours)),
            project="alpha-engine",
            work=f"alpha-engine#{number}",
            outcome="done",
            finished=AT + timedelta(hours=hours, minutes=220),
        )


def test_the_kept_runs_read_back_the_latest_first(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept_runs(database)

    assert [run.work for run in recent(database)] == ["alpha-engine#387", "alpha-engine#386"]
    assert [run.work for run in recent(database, 1)] == ["alpha-engine#387"]
    assert (recent(database)[0].phases, recent(database)[0].deliveries) == (2, 11)


def test_a_work_is_found_by_its_full_name_or_by_its_number(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept_runs(database)

    for name in ("alpha-engine#386", "#386", "386"):
        assert [run.work for run in of_work(database, name)] == ["alpha-engine#386"], name
    assert of_work(database, "38") == []


def test_a_kept_run_s_steps_read_back_in_the_order_they_happened(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    record(
        database,
        DECIDED,
        project="alpha-engine",
        work="alpha-engine#386",
        outcome="stopped",
        finished=AT,
    )
    [run] = recent(database)

    steps = steps_of(database, run.run_id)

    assert [(step.step, step.round, step.decision, step.decided_by) for step in steps] == [
        ("review", 1, "back", "review"),
        ("reviewed", 1, "back", "review"),
        ("review", 2, "", ""),
    ]


def test_nothing_kept_reads_as_nothing(tmp_path: Path) -> None:
    assert recent(tmp_path / "halyard.db") == []
    assert of_work(tmp_path / "halyard.db", "#386") == []
    assert not (tmp_path / "halyard.db").exists(), "reading made no database"
