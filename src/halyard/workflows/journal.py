"""What a finished run did, kept to be read later, and said in a line or two.

A run's file is where it has got to, and it is gone the moment the run ends.
What it did — which steps, in which phase, how many rounds each, which agent,
when — is worth more after the end than during it: that is when somebody asks
how a piece of work actually went, and across many runs how work goes at all.

**Written where the tokens are.** `halyard.core.usage` keeps what the turns
Halyard starts used — the checks a step runs, among them — in the database the
audit log lives in. The runs go into two tables beside it, so the two join on
the project and the time a step was delivered:

    SELECT s.step, s.phase, s.round, s.agent, SUM(u.output_tokens)
    FROM workflow_steps s JOIN workflow_runs r USING (run_id)
    JOIN turn_usage u ON u.project = r.project
     AND u.recorded_at BETWEEN s.at AND r.finished_at
    GROUP BY s.run_id, s.step, s.phase, s.round;

What an agent said is not kept, as the audit log does not keep it: the steps
are the record, and the conversation stays where it happened.

**Kept whether a run finished or was stopped.** A run somebody stopped part-way
is as much a fact about how work goes as one that ran to the end; `outcome`
says which. Writing never fails the run it describes.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from halyard.workflows.runs import Run

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id      TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    work        TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    phases      INTEGER NOT NULL,
    deliveries  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id TEXT NOT NULL,
    at     TEXT NOT NULL,
    step   TEXT NOT NULL,
    phase  INTEGER,
    round  INTEGER NOT NULL,
    agent  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workflow_steps_run_idx ON workflow_steps (run_id);
CREATE INDEX IF NOT EXISTS workflow_steps_at_idx ON workflow_steps (at);
"""


def _upgraded(db: sqlite3.Connection) -> None:
    """The tables as this version writes them. `workflow_steps.seat` is
    `agent` now, its rows kept: a seat was what an agent was called until
    2026-09-24. The column keeps its place, so rows go in as they did."""
    db.executescript(_SCHEMA)
    have = {row[1] for row in db.execute("PRAGMA table_info(workflow_steps)")}
    if "seat" in have and "agent" not in have:
        try:
            db.execute("ALTER TABLE workflow_steps RENAME COLUMN seat TO agent")
        except sqlite3.OperationalError as error:
            # Somebody else got here first.
            if "no such column" not in str(error) and "duplicate column" not in str(error):
                raise


def run_id(run: Run, work: str) -> str:
    """One run of one piece of work: the work, and when the run started."""
    return f"{work} {run.since.isoformat()}"


def _step_and_phase(key: str) -> tuple[str, int | None]:
    """`discover@2` as the step and its phase; a step outside the phases has none."""
    name, _, phase = key.partition("@")
    return (name, int(phase)) if phase.isdigit() else (key, None)


def deliveries(run: Run) -> list[tuple[datetime, str, int | None, int, str]]:
    """Every step the run delivered, oldest first: when, which step, which
    phase, which round of it, to which agent."""
    found = [
        (entry.at, *_step_and_phase(key), place + 1, entry.to)
        for key, entries in run.rounds.items()
        for place, entry in enumerate(entries)
    ]
    return sorted(found, key=lambda delivered: delivered[0])


def record(
    path: Path, run: Run, *, project: str, work: str, outcome: str, finished: datetime
) -> None:
    """Keep what this run did. Never raises: a record that cannot be kept costs
    the record, never the run."""
    steps = deliveries(run)
    phases = max((phase for _, _, phase, _, _ in steps if phase is not None), default=0)
    ident = run_id(run, work)
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            _upgraded(db)
            db.execute(
                "INSERT OR REPLACE INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ident,
                    project,
                    work,
                    run.workflow,
                    run.since.isoformat(),
                    finished.isoformat(),
                    outcome,
                    phases,
                    len(steps),
                ),
            )
            db.execute("DELETE FROM workflow_steps WHERE run_id = ?", (ident,))
            db.executemany(
                "INSERT INTO workflow_steps VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (ident, at.isoformat(), step, phase, number, seat)
                    for at, step, phase, number, seat in steps
                ],
            )
            db.commit()
    except sqlite3.Error:
        logger.warning(
            "Could not keep what workflow %s did in %s", run.workflow, path, exc_info=True
        )


def steps_line(run: Run, *, flow: Sequence[str], stretch: tuple[int, int] | None) -> str:
    """The steps a run took, in the flow's order, each once with its rounds —
    `to_nav · review x2 · phase 1: discover, develop · phase 2: develop · close`.

    The order a run went in is the flow's, and going back and forth is what the
    `x2` says; the database keeps every delivery in the order it happened.
    """
    taken = run.taken()

    def said(name: str, key: str) -> str:
        count = taken.get(key, 0)
        return name if count == 1 else f"{name} x{count}"

    def once(names: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(names))

    first, last = stretch if stretch is not None else (len(flow), len(flow) - 1)
    parts = [said(name, name) for name in once(flow[:first]) if taken.get(name)]
    phases = sorted(
        {phase for key in taken for _, phase in [_step_and_phase(key)] if phase is not None}
    )
    inside = once(flow[first : last + 1])
    for phase in phases:
        went = [said(name, f"{name}@{phase}") for name in inside if taken.get(f"{name}@{phase}")]
        if went:
            parts.append(f"phase {phase}: {', '.join(went)}")
    parts += [
        said(name, name)
        for name in once(flow[last + 1 :])
        if taken.get(name) and name not in flow[:first]
    ]
    return " · ".join(parts) or "no step reached its agent"


def lasted(since: datetime, until: datetime) -> str:
    """How long, the way somebody says it: `38 min`, `3 h 38 min`, `2 d 4 h`."""
    minutes = max(0, round((until - since).total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"
