"""The commands each project trusts to run without a card, kept in the database.

`runs:` in `halyard.yaml` is one place to write them. This is the other, for
the ones added without editing that file: by `halyard rules add`, by `import`
from another machine, and — when suggestions come — by a button under one.
Both apply, and `halyard rules` says which each came from. Kept beside the
audit log, which is where the evidence for adding one comes from.

**Checked on the way in.** Every entry is a `reads.run_entry`, so one that
could run anything is refused before it is kept. One kept under older rules
that no longer takes it is skipped when read back, with a warning, rather than
trusted.

**By project name, not path.** The same project lives in different places on
different machines, and an export is for the other machine.

**Read on every question.** The service asks this each time a command could
be one of them, so an entry added from the command line applies at once,
without a restart — and a database that cannot be read trusts nothing.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from halyard.core.reads import run_entry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trusted_runs (
    project  TEXT NOT NULL,
    entry    TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL,
    PRIMARY KEY (project, entry)
);
"""


def entries(path: Path, project: str | None = None) -> dict[str, tuple[str, ...]]:
    """Every kept entry by project, oldest first — or one project's. Empty when
    nothing is kept yet, or the database cannot be read."""
    if not path.is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript(_SCHEMA)
            return read(db, project)
    except sqlite3.Error:
        logger.warning("Could not read the trusted runs in %s", path, exc_info=True)
        return {}


def read(db: sqlite3.Connection, project: str | None = None) -> dict[str, tuple[str, ...]]:
    """The same, from a connection the caller opened — read-only, for one that
    must write nothing. A database with no table yet has kept nothing."""
    try:
        rows = db.execute(
            "SELECT project, entry FROM trusted_runs "
            + ("WHERE project = ? " if project is not None else "")
            + "ORDER BY added_at, rowid",
            (project,) if project is not None else (),
        ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return {}
        raise
    found: dict[str, list[str]] = {}
    for name, entry in rows:
        try:
            run_entry(entry)
        except ValueError as why:
            logger.warning("Skipping a kept entry for %s that no longer stands: %s", name, why)
            continue
        found.setdefault(name, []).append(entry)
    return {name: tuple(kept) for name, kept in found.items()}


def add(path: Path, project: str, entry: str, *, by: str, now: datetime | None = None) -> bool:
    """Keep one entry for a project. False when it was kept already.

    Raises `ValueError` for an entry that could run anything — the reason is
    `reads.run_entry`'s — and `sqlite3.Error` when it cannot be written.
    """
    text = entry.strip()
    run_entry(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(_SCHEMA)
        added = db.execute(
            "INSERT OR IGNORE INTO trusted_runs VALUES (?, ?, ?, ?)",
            (project, text, (now or datetime.now(UTC)).isoformat(), by),
        ).rowcount
        db.commit()
    return added == 1


def remove(path: Path, project: str, entry: str) -> bool:
    """Stop keeping one entry. False when it was not kept here."""
    if not path.is_file():
        return False
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(_SCHEMA)
        removed = db.execute(
            "DELETE FROM trusted_runs WHERE project = ? AND entry = ?", (project, entry.strip())
        ).rowcount
        db.commit()
    return removed > 0


def export_text(by_project: Mapping[str, Sequence[str]]) -> str:
    """Every project's entries, as `import` reads them back."""
    listed = {name: list(kept) for name, kept in sorted(by_project.items()) if kept}
    return yaml.safe_dump({"runs": listed}, sort_keys=False, allow_unicode=True)


def import_text(text: str) -> dict[str, list[str]]:
    """What an export lists, by project. `ValueError` for a file of another
    shape — refused whole, since half of a list read is a list nobody wrote."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"not YAML: {error}") from None
    runs = loaded.get("runs") if isinstance(loaded, dict) else None
    if not isinstance(runs, dict):
        raise ValueError("expected `runs:` with a list of commands under each project")
    listed: dict[str, list[str]] = {}
    for name, kept in runs.items():
        if not isinstance(kept, list) or not all(isinstance(entry, str) for entry in kept):
            raise ValueError(f"`runs: {name}:` must be a list of commands")
        listed[str(name)] = [entry.strip() for entry in kept if entry.strip()]
    return listed
