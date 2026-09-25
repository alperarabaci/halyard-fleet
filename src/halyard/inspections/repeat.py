"""A kept inspection run given again to another model, and the runs side by side.

An experiment, kept apart from the work. A repeat takes a row `record` kept —
what the model was given, word for word — and gives exactly that to another
model, in the project's directory, with the same tools. Its answer goes into
the same table beside the original, marked `experimental` and pointing back
with `repeat_of`, so it never mixes with the runs the work made. Nothing is
posted anywhere and nothing is labelled: the result is read from the table, or
from the files whoever judges the runs is handed.

**The state is recorded, not restored.** A repeat runs against the files as
they are when it runs. The envelope inside its input still says where they
stood the first time; the row's own context says where they stand now, so a
comparison can tell whether both runs saw the same code instead of this
pretending to put it back.

Through `Asker`, like every inspection: which runtime answers is the caller's
business — see `halyard.inspect_cli`.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from halyard.inspections import record
from halyard.inspections.spec import Asker, Kept, StoppedError
from halyard.inspections.turn import finding

#: How long one repeat may take — the bound an inspection has in the work.
TIMEOUT_SECONDS = 600.0

_COLUMNS = (
    "id, at, project, work, inspection, file, file_version, transition, workflow_run, step, "
    "phase, round, runtime, model, head, content, context, note, input, answer, outcome, "
    "why, finding, took, experimental, repeat_of, effort"
)


@dataclass(frozen=True)
class Row:
    """One inspection run, as `record` keeps it."""

    id: str
    at: datetime
    project: str
    work: str | None
    inspection: str
    file: str
    file_version: str
    transition: str | None
    workflow_run: str | None
    step: str | None
    phase: int | None
    round: int | None
    runtime: str | None
    model: str
    head: str | None
    content: str | None
    context: str
    note: str
    input: str
    answer: str | None
    outcome: str
    why: str
    finding: str | None
    took: float
    experimental: bool
    repeat_of: str | None
    #: None when the runtime was left to choose — every run kept before
    #: effort was, among them.
    effort: str | None


def _read(path: Path, query: str, params: Sequence[object] = ()) -> list[tuple]:
    """Rows from the database, or none — no file, no table yet, or a file that
    cannot be read all mean nothing was kept here. A table from before a
    column was added is brought up to date first, so it can be read at all."""
    if not path.is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            record.upgrade(db)
            return db.execute(query, tuple(params)).fetchall()
    except sqlite3.Error:
        return []


def _rows(path: Path, where: str, params: Sequence[object] = ()) -> list[Row]:
    return [
        Row(row[0], datetime.fromisoformat(row[1]), *row[2:24], bool(row[24]), *row[25:27])
        for row in _read(path, f"SELECT {_COLUMNS} FROM inspection_runs {where}", params)
    ]


def recent(path: Path, limit: int = 10) -> list[Row]:
    """The runs the work made, newest first. Repeats are left out; `repeats`
    counts them."""
    return _rows(path, "WHERE experimental = 0 ORDER BY at DESC LIMIT ?", (limit,))


def find(path: Path, prefix: str) -> Row | None:
    """The run an id names — or any start of one that only one id has.

    Raises `ValueError` when several do, rather than choosing between them.
    """
    wanted = prefix.strip()
    if not wanted:
        return None
    found = _rows(path, "WHERE substr(id, 1, length(?)) = ? ORDER BY at", (wanted, wanted))
    if len(found) > 1:
        raise ValueError(f"{len(found)} runs' ids start {wanted!r}; give more of it")
    return found[0] if found else None


def family(path: Path, original: str) -> list[Row]:
    """A run and every repeat of it, the original first."""
    return _rows(
        path,
        "WHERE id = ? OR repeat_of = ? ORDER BY id != ?, at",
        (original, original, original),
    )


def repeats(path: Path, ids: Sequence[str]) -> dict[str, int]:
    """How many times each of these runs has been repeated. A run never
    repeated is not in it."""
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    found = _read(
        path,
        f"SELECT repeat_of, COUNT(*) FROM inspection_runs WHERE repeat_of IN ({marks}) "
        "GROUP BY repeat_of",
        ids,
    )
    return {ident: count for ident, count in found}


async def repeat(
    original: Row,
    *,
    asker: Asker,
    model: str,
    runtime: str,
    project: Path,
    context: Sequence[str],
    findings: Sequence[str],
    database: Path,
    effort: str | None = None,
    session: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Row | None:
    """Give `original`'s input to `model` once, and keep what comes back beside it.

    `context` is where the files stand as it runs, one fact to a line, as
    `halyard.frame.context` gives it; the model reads the original's envelope,
    inside the input. `effort` is how hard it thinks; None leaves that to the
    runtime. `session` is the id the turn runs under — chosen by the caller
    when it wants to say it first, because a command the turn asks to run is a
    card that shows it.

    The row it keeps: the original's inspection, file revision, input, note,
    project and work; this run's model, effort, runtime, answer, timing and
    state. It ran for no transition and no workflow step, whatever the original
    did — those are the original's, one `repeat_of` away. None when it could
    not be kept.
    """
    session = session or str(uuid.uuid4())
    at = datetime.now(UTC)
    started = time.monotonic()
    why = ""
    try:
        said = await asker.ask(
            original.input,
            model=model,
            timeout=timeout,
            cwd=project,
            name=original.inspection,
            edits=False,
            session_id=session,
            effort=effort,
        )
    except StoppedError as stopped:
        said, why = None, str(stopped)
    except Exception as failed:
        said, why = None, f"it failed: {failed}"
    took = time.monotonic() - started
    if not said and not why:
        why = "the model did not answer"
    record.keep(
        database,
        Kept(
            session=session,
            at=at,
            name=original.inspection,
            path=Path(original.file),
            # The instructions are inside the input: the revision the original
            # ran with, whatever the file says now.
            version=original.file_version,
            transition="",
            model=model,
            asked=original.input,
            context=tuple(context),
            note=original.note,
            answer=said or None,
            why=why,
            finding=finding(said, findings) if said else None,
            took=took,
            effort=effort,
        ),
        project=original.project,
        work=original.work,
        runtime=runtime or None,
        experimental=True,
        repeat_of=original.id,
    )
    return find(database, session)


def _used(path: Path, ids: Sequence[str]) -> dict[str, tuple[int, int, float]]:
    """What each run used, by its id: the context it read, what it wrote, and
    the list price. A run whose turn recorded nothing is not in it."""
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    found = _read(
        path,
        "SELECT session_id, SUM(input_tokens + cache_write_tokens + cache_read_tokens), "
        f"SUM(output_tokens), SUM(cost_usd) FROM turn_usage WHERE session_id IN ({marks}) "
        "GROUP BY session_id",
        ids,
    )
    return {ident: (read or 0, wrote or 0, cost or 0.0) for ident, read, wrote, cost in found}


def compared(path: Path, runs: Sequence[Row]) -> list[str]:
    """A run and its repeats as a table: model, effort, time, outcome, finding,
    tokens.

    `effort` is `default` where the runtime was left to choose. `same` says
    whether a repeat saw the files where the original did: `yes` when `HEAD`
    and the content id both match, `no` when either differs, `?` when one of
    them was not recorded.
    """
    if not runs:
        return []
    first = runs[0]
    used = _used(path, [run.id for run in runs])
    table = [
        (
            "",
            "id",
            "model",
            "effort",
            "runtime",
            "took",
            "outcome",
            "finding",
            "read",
            "wrote",
            "$",
            "same",
        )
    ]
    for place, run in enumerate(runs):
        read, wrote, cost = used.get(run.id, (0, 0, 0.0))
        if place == 0:
            same = "-"
        elif run.head and run.content and first.head and first.content:
            same = "yes" if (run.head, run.content) == (first.head, first.content) else "no"
        else:
            same = "?"
        table.append(
            (
                "original" if place == 0 else f"repeat {place}",
                run.id[:8],
                run.model,
                run.effort or "default",
                run.runtime or "?",
                f"{run.took:.0f}s",
                run.outcome,
                run.finding or "-",
                f"{read:,}" if read else "-",
                f"{wrote:,}" if wrote else "-",
                f"{cost:.2f}" if cost else "-",
                same,
            )
        )
    widths = [max(len(row[column]) for row in table) for column in range(len(table[0]))]
    where = first.work or first.project
    ran_for = (
        f" · step {first.step}"
        if first.step
        else f" · transition {first.transition}"
        if first.transition
        else ""
    )
    return [
        f"{first.inspection} on {where}{ran_for} · {first.at.astimezone():%Y-%m-%d %H:%M}",
        "",
        *(
            "  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip()
            for row in table
        ),
    ]


def write(into: Path, runs: Sequence[Row], table: Sequence[str]) -> None:
    """The runs as files, for whoever judges them: the input once, the table,
    and every answer in a file of its own — or why there is none."""
    into.mkdir(parents=True, exist_ok=True)
    (into / "input.md").write_text(runs[0].input, encoding="utf-8")
    (into / "runs.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    for place, run in enumerate(runs):
        name = "original" if place == 0 else f"repeat-{place}"
        # A model can be named by its provider as well — `zai/glm-4.6`.
        model = re.sub(r"[^A-Za-z0-9._-]", "_", run.model)
        if run.effort:
            model += "-" + re.sub(r"[^A-Za-z0-9._-]", "_", run.effort)
        said = run.answer if run.answer is not None else f"(no answer: {run.why})"
        (into / f"{place:02d}-{name}-{model}-{run.id[:8]}.md").write_text(said, encoding="utf-8")
