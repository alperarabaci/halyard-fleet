"""Every inspection run, kept whole, beside the tokens it used.

An inspection is the easiest thing Halyard does to compare across models: one
input, one output, nothing it changes. That only helps if the input is kept —
what the model was given, word for word — along with what it said, how long it
took, and where the files stood. So each run is a row, whichever way it ended,
and one that is run again later with another model is a row beside it.

**Joined, not copied.** The row's id is the session the turn ran under, which is
also what `halyard.core.usage` records its tokens by, so what an inspection cost
is one join away:

    SELECT r.inspection, r.model, r.took, u.output_tokens, u.cache_read_tokens
    FROM inspection_runs r JOIN turn_usage u ON u.session_id = r.id;

An inspection a workflow step ran carries that step — its run, name, phase and
round — so it joins `workflow_steps` the same way.

**The text is kept, on purpose.** Halyard keeps no conversation anywhere else.
Here the reply that was inspected is part of the input, and an inspection
cannot be compared or run again without it. `experimental` and `repeat_of` are
for the runs somebody makes on purpose to compare, so they never mix with the
ones the work made.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

from halyard.inspections.spec import Kept

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inspection_runs (
    id           TEXT PRIMARY KEY,
    at           TEXT NOT NULL,
    project      TEXT NOT NULL,
    work         TEXT,
    inspection   TEXT NOT NULL,
    file         TEXT NOT NULL,
    file_version TEXT NOT NULL,
    transition   TEXT,
    workflow_run TEXT,
    step         TEXT,
    phase        INTEGER,
    round        INTEGER,
    runtime      TEXT,
    model        TEXT NOT NULL,
    effort       TEXT,
    head         TEXT,
    content      TEXT,
    context      TEXT NOT NULL,
    note         TEXT NOT NULL,
    input        TEXT NOT NULL,
    answer       TEXT,
    outcome      TEXT NOT NULL,
    why          TEXT NOT NULL,
    finding      TEXT,
    took         REAL NOT NULL,
    experimental INTEGER NOT NULL DEFAULT 0,
    repeat_of    TEXT
);

CREATE INDEX IF NOT EXISTS inspection_runs_at_idx ON inspection_runs (at);
CREATE INDEX IF NOT EXISTS inspection_runs_work_idx ON inspection_runs (work);
"""

#: Columns added after the table was first made, and what they hold. A table
#: made before one of them gets it added, empty for the rows it already has.
_ADDED = {"effort": "TEXT"}

#: Columns renamed since, by the name they had: a transition was a handoff
#: until 2026-09-24.
_RENAMED = {"handoff": "transition"}


def upgrade(db: sqlite3.Connection) -> None:
    """The table as this version reads and writes it, made or brought up to
    date. `effort` came a day after the table did: the runs kept before it
    ran at whatever effort the runtime chose, and say nothing. `handoff` is
    `transition` now, its rows kept."""
    db.executescript(_SCHEMA)
    have = {row[1] for row in db.execute("PRAGMA table_info(inspection_runs)")}
    # The service and `halyard inspect` can both get to each of these first,
    # and the second finds it done.
    for old, new in _RENAMED.items():
        if old in have and new not in have:
            try:
                db.execute(f"ALTER TABLE inspection_runs RENAME COLUMN {old} TO {new}")
            except sqlite3.OperationalError as error:
                if "no such column" not in str(error) and "duplicate column" not in str(error):
                    raise
            have = (have - {old}) | {new}
    for column, kind in _ADDED.items():
        if column in have:
            continue
        try:
            db.execute(f"ALTER TABLE inspection_runs ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError as error:
            if "duplicate column" not in str(error):
                raise
    db.commit()


def _said(context: tuple[str, ...], label: str) -> str | None:
    """The first word after `label` in the envelope, where the files stood —
    `HEAD: 2cdcab9c` or `Content: 5df40c87aff3 (…)`."""
    for line in context:
        if line.startswith(f"{label}: "):
            words = line.removeprefix(f"{label}: ").split()
            return words[0] if words else None
    return None


def keep(
    path: Path,
    kept: Kept,
    *,
    project: str,
    work: str | None,
    runtime: str | None,
    workflow_run: str | None = None,
    step: str | None = None,
    phase: int | None = None,
    round: int | None = None,
    experimental: bool = False,
    repeat_of: str | None = None,
) -> None:
    """Write one inspection run. Never raises: a record that cannot be kept
    costs the record, never the inspection."""
    row = {
        "id": kept.session,
        "at": kept.at.isoformat(),
        "project": project,
        "work": work,
        "inspection": kept.name,
        "file": str(kept.path),
        "file_version": kept.version,
        "transition": kept.transition or None,
        "workflow_run": workflow_run,
        "step": step,
        "phase": phase,
        "round": round,
        "runtime": runtime,
        "model": kept.model,
        "effort": kept.effort,
        "head": _said(kept.context, "HEAD"),
        "content": _said(kept.context, "Content"),
        "context": "\n".join(kept.context),
        "note": kept.note,
        "input": kept.asked,
        "answer": kept.answer,
        "outcome": "answered" if kept.answer is not None else "unmeasured",
        "why": kept.why,
        "finding": kept.finding,
        "took": kept.took,
        "experimental": int(experimental),
        "repeat_of": repeat_of,
    }
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            upgrade(db)
            # By name: a table brought up to date has its newer columns last.
            db.execute(
                f"INSERT OR REPLACE INTO inspection_runs ({', '.join(row)}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
            db.commit()
    except sqlite3.Error:
        logger.warning("Could not keep inspection %s in %s", kept.name, path, exc_info=True)
