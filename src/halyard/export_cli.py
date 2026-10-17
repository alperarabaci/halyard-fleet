"""`halyard export` — everything this machine's Halyard keeps, in one dated zip.

What Halyard knows lives beside it, in files the repository ignores: the
configuration, the database of approvals, runs and usage, the audit log, your
prompts, the kept inspections, the logs. None of it is in git, so a disk that
fails, a checkout deleted to start clean or a mistaken edit takes it with it.

This copies all of it into `export/halyard-state-<stamp>.zip`, beside the
checkout and ignored like the rest:

- the database is read with SQLite's own backup, not copied as a file, so a
  service writing to it while this runs still gives a consistent copy;
- the zip holds the bot token and any other secret in `halyard.yaml`, so it is
  written readable by its owner only, and says so;
- `.bak` copies are left out — they are older versions of the same files, and
  they hold old secrets;
- the files `halyard.yaml` points at inside each project go in too, under
  `project-files/<project>/` — a transition's prompt, an inspection, the
  confirmation round's, an agent's compaction files. They live in the project,
  wherever it keeps them, and are as much what makes this setup as the YAML.

Putting one back is unzipping it into a checkout with the service stopped.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

#: Single files that are state, by name, where Halyard keeps them.
STATE_FILES = (
    "halyard.yaml",
    "halyard.yml",
    ".env",
    ".env.local",
    "credential-seen.json",
    "last-said.json",
    "check-results.json",
    "halyard.log",
    "bridge.log",
)

#: Folders that are state, whole.
STATE_FOLDERS = ("prompts", "projects", "compaction_hook", "logs")

#: Where exports go, in the checkout. Ignored by git.
FOLDER = "export"

Say = Callable[[str], None]


def main(argv: list[str]) -> int:
    if argv:
        print("usage: halyard export", file=sys.stderr)
        return 2
    database, audit = _paths()
    export(Path.cwd(), database=database, audit=audit)
    return 0


def export(
    root: Path,
    *,
    database: Path,
    audit: Path,
    now: str | None = None,
    say: Say = print,
) -> Path:
    """Write the zip, and return where it went."""
    stamp = now or datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / FOLDER / f"halyard-state-{stamp}.zip"
    target.parent.mkdir(exist_ok=True)
    # Owner-only from the first byte, not narrowed once written: the zip holds
    # the bot token.
    target.touch(mode=0o600)

    taken: list[str] = []
    with (
        tempfile.TemporaryDirectory(prefix="halyard-export-") as scratch,
        zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        if (database := _absolute(root, database)).is_file():
            snapshot = Path(scratch) / "halyard.db"
            _snapshot(database, snapshot)
            archive.write(snapshot, "halyard.db")
            taken.append("halyard.db")
        if (audit := _absolute(root, audit)).is_file():
            archive.write(audit, "audit.jsonl")
            taken.append("audit.jsonl")
        for name in (*STATE_FILES, *(p.name for p in sorted(root.glob("halyard.log.*")))):
            if (root / name).is_file() and not name.endswith(".bak"):
                archive.write(root / name, name)
                taken.append(name)
        for folder in STATE_FOLDERS:
            files = [p for p in sorted((root / folder).rglob("*")) if p.is_file()]
            for path in files:
                if path.suffix != ".bak":
                    archive.write(path, path.relative_to(root).as_posix())
            if files:
                taken.append(f"{folder}/")
        for project, found in _project_files(root).items():
            for inside, path in found:
                archive.write(path, f"project-files/{project}/{inside}")
            if found:
                taken.append(f"project-files/{project}/ ({len(found)})")
    os.chmod(target, 0o600)

    size = target.stat().st_size
    say(f"Wrote {target} ({_size(size)}): {', '.join(taken) or 'nothing to keep yet'}.")
    say("It holds your bot token and any other secret in halyard.yaml: keep it to yourself.")
    return target


def _project_files(root: Path) -> dict[str, list[tuple[str, Path]]]:
    """The files each project's configuration points at, that are there: where
    each goes in the zip, and where it is. Nothing when the configuration
    cannot be read — the rest of the backup is worth more than these."""
    from halyard.core.config_file import find_config, projects_from_yaml

    config = find_config(root)
    try:
        projects = projects_from_yaml(config.read_text(encoding="utf-8")) if config else []
    except (OSError, ValueError):
        return {}
    found: dict[str, list[tuple[str, Path]]] = {}
    for project in projects:
        named = [
            *(transition.prompt for transition in project.transitions.values()),
            *(transition.followup_prompt for transition in project.transitions.values()),
            *project.inspections.values(),
            *(
                (project.confirmation.inquiry, project.confirmation.review)
                if project.confirmation
                else ()
            ),
            *(seat.after_compaction for seat in project.seats),
            *(seat.before_compaction for seat in project.seats),
        ]
        home = project.path.expanduser() if project.path else None
        kept: dict[str, Path] = {}
        for written in filter(None, named):
            path = Path(written).expanduser()
            for base in (home, root):
                where = path if path.is_absolute() else (base / path if base else None)
                if where is not None and where.is_file():
                    inside = (
                        where.relative_to(home)
                        if home and where.is_relative_to(home)
                        else Path("_elsewhere") / where.relative_to(where.anchor)
                    )
                    kept.setdefault(inside.as_posix(), where)
                    break
        found[project.name] = sorted(kept.items())
    return found


def _snapshot(database: Path, into: Path) -> None:
    """A consistent copy of a database that may be being written to."""
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(into)) as copy:
        source.backup(copy)


def _absolute(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _paths() -> tuple[Path, Path]:
    """The database and the audit log, where this machine's settings put them —
    the defaults when the settings cannot be read, which is when a backup is
    wanted most."""
    try:
        from halyard.config import Settings

        settings = Settings()
        return settings.db_path, settings.audit_log
    except Exception:
        return Path("halyard.db"), Path("audit.jsonl")


def _size(count: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if count < 1024 or unit == "MB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"
