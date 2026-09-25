"""`halyard rules` — the commands each project trusts to run without a card.

Reads both places they are written — `runs:` in `halyard.yaml`, and the table
`halyard.core.trusted_runs` keeps — and changes only the second: the file is
somebody's to edit, and a tool that rewrote it would lose their comments and
their order. See `docs/setup.md` for how an entry is written.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from halyard.core import trusted_runs
from halyard.core.config_file import Project

USAGE = """usage: halyard rules [list [project] | add <project> <entry> | remove <project> <entry>
                     | export [file] | import <file>]

  list [project]              what runs without asking in each project, and where
                              it is written: halyard.yaml, or here
  add <project> "<entry>"     trust a command in a project, written as the agent
                              types it: "uv run pytest *"
  remove <project> "<entry>"  stop trusting one added here; one in halyard.yaml is
                              removed there
  export [file]               every project's list, for `import` on another machine
  import <file>               add what an export lists

Quote an entry: its `*` is for Halyard, not for your shell. An entry that could run
anything is refused. They apply at once, and only with HALYARD_ALLOW_RISK_AT_OR_BELOW
set to low.
"""


def main(args: Sequence[str]) -> int:
    """`halyard rules …` — see `USAGE`."""
    words = list(args)
    what, rest = (words[0], words[1:]) if words else ("list", [])
    expected = {"list": (0, 1), "add": (2,), "remove": (2,), "export": (0, 1), "import": (1,)}
    if len(rest) not in expected.get(what, ()):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        described = _projects()
    except ValueError as error:
        print(f"halyard rules: {error}", file=sys.stderr)
        return 2
    database = _database()
    if what == "list":
        return _list(database, described, rest[0] if rest else None)
    if what == "export":
        return _export(database, described, Path(rest[0]) if rest else None)
    if what == "import":
        return _import(database, described, Path(rest[0]))
    project, entry = rest
    if project not in described:
        known = ", ".join(sorted(described)) or "none"
        print(
            f"halyard rules: no project {project!r} in halyard.yaml (known: {known})",
            file=sys.stderr,
        )
        return 2
    if what == "add":
        return _add(database, described[project], entry)
    return _remove(database, described[project], entry)


def _database() -> Path:
    """Where the control plane keeps its database, read the way it reads it."""
    try:
        from halyard.config import Settings

        return Settings().db_path
    except Exception:
        return Path("./halyard.db")


def _projects() -> dict[str, Project]:
    from halyard.core.config_file import projects

    return {project.name: project for project in projects()}


def _switched_on() -> bool:
    try:
        from halyard.config import Settings

        return bool(Settings().allow_risk_at_or_below)
    except Exception:
        return False


def _list(database: Path, described: dict[str, Project], only: str | None) -> int:
    kept = trusted_runs.entries(database)
    names = sorted(set(described) | set(kept))
    if only is not None:
        if only not in names:
            print(f"halyard rules: no project {only!r}", file=sys.stderr)
            return 2
        names = [only]
    shown = False
    for name in names:
        project = described.get(name)
        written = project.runs if project else ()
        refused = project.runs_refused if project else ()
        here = [entry for entry in kept.get(name, ()) if entry not in written]
        if not (written or refused or here):
            continue
        shown = True
        print(name + ("" if project else "  (not in halyard.yaml, so none of these apply)"))
        rows = [(entry, "halyard.yaml") for entry in written]
        rows += [(entry, "halyard rules") for entry in here]
        rows += [(entry, f"ignored: {why}") for entry, why in refused]
        width = max(len(entry) for entry, _ in rows)
        for entry, where in rows:
            print(f"  {entry.ljust(width)}  {where}")
    if not shown:
        print("No project trusts any command to run without asking yet.")
    elif not _switched_on():
        print("\nHALYARD_ALLOW_RISK_AT_OR_BELOW is not set, so none of these apply yet.")
    return 0


def _add(database: Path, project: Project, entry: str) -> int:
    if entry.strip() in project.runs:
        print(f"{project.name} already trusts `{entry.strip()}`, in halyard.yaml")
        return 0
    try:
        added = trusted_runs.add(database, project.name, entry, by="halyard rules")
    except ValueError as why:
        print(f"halyard rules: not added — {why}", file=sys.stderr)
        return 2
    except sqlite3.Error as error:
        print(f"halyard rules: could not write to {database}: {error}", file=sys.stderr)
        return 1
    said = "now trusts" if added else "already trusts"
    print(f"{project.name} {said} `{entry.strip()}` — from the next command on")
    return 0


def _remove(database: Path, project: Project, entry: str) -> int:
    try:
        removed = trusted_runs.remove(database, project.name, entry)
    except sqlite3.Error as error:
        print(f"halyard rules: could not write to {database}: {error}", file=sys.stderr)
        return 1
    if removed:
        print(f"{project.name} no longer trusts `{entry.strip()}`")
        return 0
    if entry.strip() in project.runs:
        print(
            f"halyard rules: `{entry.strip()}` is written in halyard.yaml; remove it there",
            file=sys.stderr,
        )
    else:
        print(f"halyard rules: {project.name} does not trust `{entry.strip()}`", file=sys.stderr)
    return 1


def _export(database: Path, described: dict[str, Project], into: Path | None) -> int:
    """Both places at once: the other machine should end up trusting what
    this one does, whichever file it was written in here."""
    kept = trusted_runs.entries(database)
    listed = {
        name: list(
            dict.fromkeys((*(p.runs if (p := described.get(name)) else ()), *kept.get(name, ())))
        )
        for name in set(described) | set(kept)
    }
    text = trusted_runs.export_text(listed)
    if into is None:
        print(text, end="")
        return 0
    try:
        into.write_text(text, encoding="utf-8")
    except OSError as error:
        print(f"halyard rules: could not write {into}: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {sum(len(v) for v in listed.values())} entries to {into}")
    return 0


def _import(database: Path, described: dict[str, Project], source: Path) -> int:
    try:
        listed = trusted_runs.import_text(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"halyard rules: could not read {source}: {error}", file=sys.stderr)
        return 2
    added = present = 0
    refused: list[str] = []
    for name, entries in listed.items():
        project = described.get(name)
        if project is None:
            refused += [f"{name}: `{entry}` — no project {name!r} here" for entry in entries]
            continue
        for entry in entries:
            if entry in project.runs:
                present += 1
                continue
            try:
                if trusted_runs.add(database, name, entry, by=f"import {source.name}"):
                    added += 1
                else:
                    present += 1
            except ValueError as why:
                refused.append(f"{name}: {why}")
            except sqlite3.Error as error:
                print(f"halyard rules: could not write to {database}: {error}", file=sys.stderr)
                return 1
    print(f"Added {added}, already trusted {present}, refused {len(refused)}")
    for line in refused:
        print(f"  refused {line}")
    return 0
