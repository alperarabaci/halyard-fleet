"""Every upkeep run, kept whole in the database beside the tokens it used.

What a job was given and what came back is worth more afterwards than while it
runs: which advice was taken, how it changed as the rules did, what a model
proposed that a person did not add. So each run is a row in `halyard.db` — the
evidence, the answer, what Halyard made of it and what it printed — under the
id its turn ran as, which is `turn_usage.session_id` for the same turn.

Written once the turn is over, never while it runs: the job reads the database
read-only and holds nothing open while a model thinks. Kept even when there
was no answer, or one of the wrong shape — how often that happens is part of
the record too. A row that cannot be written costs the row, never the run.

What is kept is Halyard's own: the evidence is its log, redacted as the log
is; the answer is advice about that. No agent's prose is in it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS upkeep_runs (
    id       TEXT PRIMARY KEY,
    at       TEXT NOT NULL,
    job      TEXT NOT NULL,
    project  TEXT NOT NULL,
    model    TEXT,
    effort   TEXT,
    days     INTEGER NOT NULL,
    took     REAL NOT NULL,
    outcome  TEXT NOT NULL,
    evidence TEXT NOT NULL,
    answer   TEXT,
    advice   TEXT,
    printed  TEXT
);
CREATE INDEX IF NOT EXISTS upkeep_runs_at_idx ON upkeep_runs (at);
"""


@dataclass(frozen=True)
class Kept:
    """One run, as the database keeps it."""

    id: str
    at: datetime
    job: str
    project: str
    model: str | None
    effort: str | None
    days: int
    took: float
    #: `answered`, `no answer` or `wrong shape`.
    outcome: str
    evidence: str
    answer: str | None
    #: What Halyard made of the answer, checked and counted, as JSON.
    advice: str | None
    #: What the person was shown.
    printed: str | None


def keep(path: Path, run: Kept) -> None:
    """Write one run down. Never raises: a row that cannot be written costs
    the row, never the run it describes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR REPLACE INTO upkeep_runs (id, at, job, project, model, effort, days, "
                "took, outcome, evidence, answer, advice, printed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.at.isoformat(),
                    run.job,
                    run.project,
                    run.model,
                    run.effort,
                    run.days,
                    run.took,
                    run.outcome,
                    run.evidence,
                    run.answer,
                    run.advice,
                    run.printed,
                ),
            )
            db.commit()
    except (OSError, sqlite3.Error):
        logger.warning("Could not keep upkeep run %s in %s", run.id, path, exc_info=True)


def _rows(path: Path, sql: str, params: tuple) -> list[Kept]:
    """Rows over a read-only connection: reading them changes nothing."""
    if not path.is_file():
        return []
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            found = db.execute(
                "SELECT id, at, job, project, model, effort, days, took, outcome, evidence, "
                "answer, advice, printed FROM upkeep_runs " + sql,
                params,
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return []
        raise
    return [
        Kept(
            id=row[0],
            at=datetime.fromisoformat(row[1]),
            job=row[2],
            project=row[3],
            model=row[4],
            effort=row[5],
            days=row[6],
            took=row[7],
            outcome=row[8],
            evidence=row[9],
            answer=row[10],
            advice=row[11],
            printed=row[12],
        )
        for row in found
    ]


def recent(path: Path, limit: int = 10) -> list[Kept]:
    """The latest runs first."""
    return _rows(path, "ORDER BY at DESC LIMIT ?", (limit,))


def find(path: Path, prefix: str) -> Kept | None:
    """The run an id names — or any start of one that only one id has.
    `ValueError` when several do, rather than choosing between them."""
    wanted = prefix.strip()
    if not wanted:
        return None
    found = _rows(path, "WHERE substr(id, 1, length(?)) = ? ORDER BY at", (wanted, wanted))
    if len(found) > 1:
        raise ValueError(f"{len(found)} runs' ids start {wanted!r}; give more of it")
    return found[0] if found else None


def proposals_of(run: Kept) -> int:
    """How many entries a run's advice proposed, for a one-line listing."""
    try:
        return len(json.loads(run.advice or "{}").get("proposals", []))
    except ValueError:
        return 0
