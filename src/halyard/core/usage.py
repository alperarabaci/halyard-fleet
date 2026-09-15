"""What the turns Halyard starts on its own use, kept beside the audit log.

Halyard asks a model for things by itself — a project's checks, a commit
message, the record a compaction is about to make unrecoverable — and a turn
nobody typed is a turn nobody watched the size of. A runtime that can say what
a turn used hands it here as `Turn`s, one per model the turn used, with what it
was for and which project. Claude Code can, from the answer it gives as JSON;
its runner does the reading, since the shape is its own.

**The tokens are the measure.** The dollar figure is Claude Code's own, worked
out at list price. On a subscription nothing is billed per token, so it says
what the same turns would cost on the API — good for comparing one kind of
turn with another, not a bill.

**Most of a turn is not the question.** Measured on 2026-09-15: a one-word
answer from sonnet read 2 tokens of question and wrote 46,103 to the prompt
cache — Claude Code's own instructions and tools, sent with every new turn.
Cache writes and reads are kept apart from input for that reason.

In the same SQLite file as the audit log, in a table of its own. Recording
never fails the turn it describes, and `halyard usage` reads it back.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: How far back `halyard usage` looks unless told otherwise.
DEFAULT_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_usage (
    sequence           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at        TEXT NOT NULL,
    runtime            TEXT NOT NULL,
    session_id         TEXT,
    model              TEXT,
    purpose            TEXT,
    project            TEXT,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cost_usd           REAL
);

CREATE INDEX IF NOT EXISTS turn_usage_recorded_at_idx ON turn_usage (recorded_at);
"""

_INSERT = """
INSERT INTO turn_usage
    (recorded_at, runtime, session_id, model, purpose, project,
     input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_TOTALS = """
SELECT COALESCE(model, '?'), COALESCE(purpose, '?'),
       COUNT(DISTINCT COALESCE(session_id, sequence)),
       SUM(input_tokens), SUM(output_tokens),
       SUM(cache_write_tokens), SUM(cache_read_tokens),
       SUM(cost_usd)
FROM turn_usage
WHERE recorded_at >= ?
GROUP BY model, purpose
ORDER BY SUM(cost_usd) DESC
"""


@dataclass(frozen=True)
class Turn:
    """What one turn used of one model."""

    runtime: str
    session_id: str | None
    model: str | None
    purpose: str | None
    project: str | None
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    #: At list price, as the runtime works it out. See the module docstring.
    cost_usd: float | None


def record(path: Path, turns: Sequence[Turn], *, at: datetime | None = None) -> None:
    """Write them down. Never raises: a record that cannot be kept costs the
    record, never the turn it describes."""
    if not turns:
        return
    when = (at or datetime.now(UTC)).isoformat()
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript(_SCHEMA)
            db.executemany(_INSERT, [(when, *astuple(turn)) for turn in turns])
            db.commit()
    except sqlite3.Error:
        logger.warning("Could not record what a turn used in %s", path, exc_info=True)


def totals(path: Path, since: datetime) -> list[tuple]:
    """Per model and purpose since `since`: turns, each kind of token, and the
    list price — the most expensive first. Empty when there is nothing yet."""
    if not path.is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript(_SCHEMA)
            return db.execute(_TOTALS, (since.isoformat(),)).fetchall()
    except sqlite3.Error:
        logger.warning("Could not read what turns used from %s", path, exc_info=True)
        return []


def report(path: Path, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> str:
    """What `halyard usage` prints: one line per model and purpose, and a total."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    rows = totals(path, since)
    span = "day" if days == 1 else f"{days} days"
    if not rows:
        return f"Nothing recorded in {path} in the last {span}."
    header = ("model", "for", "turns", "input", "output", "cache write", "cache read", "list $")
    table = [header]
    for model, purpose, *counts, cost in rows:
        table.append((model, purpose, *(f"{n or 0:,}" for n in counts), f"{cost or 0:.2f}"))
    summed = [sum(row[i] or 0 for row in rows) for i in range(2, 8)]
    table.append(("total", "", *(f"{n:,}" for n in summed[:-1]), f"{summed[-1]:.2f}"))
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = [f"Turns Halyard started itself in the last {span} ({path}):", ""]
    for row in table:
        text = [row[i].ljust(widths[i]) for i in (0, 1)]
        text += [row[i].rjust(widths[i]) for i in range(2, len(header))]
        lines.append("  " + "  ".join(text).rstrip())
    lines += [
        "",
        "Tokens are the measure. `list $` is Claude Code's own figure at API list",
        "price: what the same turns would cost there, not what a subscription bills.",
    ]
    return "\n".join(lines)


def main(args: Sequence[str]) -> int:
    """`halyard usage [days]`."""
    try:
        days = int(args[0]) if args else DEFAULT_DAYS
    except ValueError:
        days = 0
    if days < 1:
        print(f"halyard usage: {args[0]!r} is not a number of days", file=sys.stderr)
        return 2
    print(report(_database(), days=days))
    return 0


def _database() -> Path:
    """Where the control plane keeps its database, read the way it reads it."""
    try:
        from halyard.config import Settings

        return Settings().db_path
    except Exception:
        return Path("./halyard.db")
