"""`halyard runs` — what workflow runs did, read back without SQL.

A run that ends is kept in `workflow_runs` and `workflow_steps`, beside the
tokens (see `halyard.workflows.journal`). This prints them: the latest runs a
line each, or one piece of work's runs step by step, with what each answer
decided and on whose word. A run not finished yet — waiting on an agent, or
stopped for somebody — is read from its project's file, so it is there before
it ends. A run is named by its work or by its own name, the eight characters
its log lines and its steps' first lines carry. It only reads.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from halyard.workflows import journal
from halyard.workflows import runs as going_runs

USAGE = """usage: halyard runs [recent [n] | <work> | <run>]

  recent [n]   the last n workflow runs (10), a line each — what plain
               `halyard runs` prints too
  <work>       every run one piece of work had, step by step: when each step
               reached its agent, which round it was, and what the answer
               decided — with the step whose word it was, when that was
               another step's. `alpha-engine#386`, `#386` and `386` name the
               same work.
  <run>        one run by its own name — `3f2a9c1e`, on its log lines and on
               the first line of each step it sent

A run is kept when it finishes or is stopped with ⏹; one still going is read
from its project's file. Steps kept before 2026-09-25 have no decisions.
"""


def main(args: Sequence[str]) -> int:
    """`halyard runs …` — see `USAGE`."""
    words = list(args)
    if not words or words[0] == "recent":
        rest = words[1:]
        if len(rest) > 1 or (rest and not (rest[0].isdigit() and int(rest[0]) > 0)):
            print(USAGE, file=sys.stderr)
            return 2
        return _recent(_database(), int(rest[0]) if rest else 10)
    if len(words) > 1 or words[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    return _work(_database(), words[0])


def _database() -> Path:
    """Where the control plane keeps its database, read the way it reads it."""
    try:
        from halyard.config import Settings

        return Settings().db_path
    except Exception:
        return Path("./halyard.db")


def _recent(database: Path, limit: int) -> int:
    runs = journal.recent(database, limit)
    if not runs:
        print(f"No workflow run is kept in {database} yet. A run is kept when it ends.")
        return 0
    rows = [
        (
            f"{run.started.astimezone():%m-%d %H:%M}",
            run.work,
            run.workflow,
            run.outcome,
            journal.lasted(run.started, run.finished),
            _counted(run),
        )
        for run in runs
    ]
    print("\n".join(_table(rows)))
    return 0


def _work(database: Path, work: str) -> int:
    """The runs `work` names — as work, or as a run's own name — kept and
    still going, oldest first."""
    kept = journal.named(database, work) or journal.of_work(database, work)
    names = {run.run_id for run in kept}
    unfinished = [
        (its_work, run)
        for _, its_work, run in going_runs.going(database.parent / "projects")
        if (run.ident and run.ident == work.strip()) or journal.same_work(its_work, work)
        if journal.run_id(run, its_work) not in names
    ]
    if not kept and not unfinished:
        print(
            f"halyard runs: no run of {work!r} is kept in {database} or still going",
            file=sys.stderr,
        )
        return 1
    printed = 0
    for run in kept:
        print(("\n" if printed else "") + _heading(run) + "\n")
        _print_steps(journal.steps_of(database, run.run_id))
        printed += 1
    for its_work, run in unfinished:
        print(("\n" if printed else "") + _going(its_work, run) + "\n")
        _print_steps(journal.deliveries(run))
        printed += 1
    return 0


def _print_steps(steps: Sequence[journal.Delivery]) -> None:
    if not steps:
        print("  no step reached its agent")
        return
    print("\n".join(f"  {line}" for line in _steps(steps)))


def _going(work: str, run: going_runs.Run) -> str:
    """A run not finished yet: where it is, as the chat's line says it."""
    if run.waiting:
        state = f"waiting for {run.waiting_for}"
    elif run.sending_to:
        state = f"on its way to {run.sending_to}"
    else:
        state = f"stopped: {run.stopped or 'not started'}"
    named = f" · run {run.ident}" if run.ident else ""
    return f"{work} · {run.workflow} · {state}\n{run.since.astimezone():%m-%d %H:%M} → now{named}"


def _counted(run: journal.Kept) -> str:
    said = f"{run.deliveries} step" + ("" if run.deliveries == 1 else "s")
    if not run.phases:
        return said
    return f"{said}, {run.phases} phase" + ("" if run.phases == 1 else "s")


def _heading(run: journal.Kept) -> str:
    """Which run, and when: two lines, as the chat's report says them."""
    start, end = run.started.astimezone(), run.finished.astimezone()
    until = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%m-%d %H:%M}"
    # A run kept since runs had names is kept under its name; one from before,
    # under its work and start, which says nothing a reader can use.
    named = "" if " " in run.run_id else f" · run {run.run_id}"
    return (
        f"{run.work} · {run.workflow} · {run.outcome}\n"
        f"{start:%m-%d %H:%M} → {until} · {journal.lasted(run.started, run.finished)}{named}"
    )


def _steps(steps: Sequence[journal.Delivery]) -> list[str]:
    """A step a line. A phase column only for a run that had phases, and the
    date only for one that went past midnight."""
    days = {step.at.astimezone().date() for step in steps}
    shape = "%H:%M" if len(days) == 1 else "%m-%d %H:%M"
    phased = any(step.phase is not None for step in steps)
    rows = [("at", "step", *(("phase",) if phased else ()), "round", "agent", "decided")]
    rows += [
        (
            f"{step.at.astimezone():{shape}}",
            step.step,
            *((str(step.phase or ""),) if phased else ()),
            str(step.round),
            step.agent,
            _decided(step),
        )
        for step in steps
    ]
    return _table(rows)


def _decided(step: journal.Delivery) -> str:
    """`back`, or `back (review)` when the run moved on another step's word."""
    if not step.decision:
        return ""
    if step.decided_by and step.decided_by != step.step:
        return f"{step.decision} ({step.decided_by})"
    return step.decision


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
