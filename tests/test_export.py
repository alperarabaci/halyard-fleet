"""`halyard export`: what Halyard keeps, in one zip that can be put back."""

from __future__ import annotations

import sqlite3
import stat
import zipfile
from contextlib import closing
from pathlib import Path

from halyard.export_cli import export


def a_machine(root: Path) -> sqlite3.Connection:
    """A checkout's state, with the database open and being written to, as a
    running service holds it."""
    (root / "halyard.yaml").write_text('settings:\n  TELEGRAM_BOT_TOKEN: "1:t"\n')
    (root / "halyard.yaml.20261017-201500.bak").write_text("an older token\n")
    (root / "audit.jsonl").write_text('{"action": "approval.requested"}\n')
    (root / "last-said.json").write_text("{}\n")
    (root / "prompts").mkdir()
    (root / "prompts" / "runs-advice.md").write_text("# mine\n")
    (root / "logs").mkdir()
    (root / "logs" / "bridge-2026-W42.log").write_text("a line\n")
    (root / "src").mkdir()
    (root / "src" / "code.py").write_text("not state\n")
    database = sqlite3.connect(root / "halyard.db")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE runs (id INTEGER)")
    database.executemany("INSERT INTO runs VALUES (?)", [(1,), (2,), (3,)])
    database.commit()
    return database


def exported(root: Path) -> Path:
    said: list[str] = []
    return export(
        root,
        database=Path("halyard.db"),
        audit=Path("audit.jsonl"),
        now="20261018-090000",
        say=said.append,
    )


def test_everything_halyard_keeps_goes_in_and_nothing_else(tmp_path: Path) -> None:
    with closing(a_machine(tmp_path)):
        target = exported(tmp_path)

    names = set(zipfile.ZipFile(target).namelist())
    assert target == tmp_path / "export" / "halyard-state-20261018-090000.zip"
    assert {
        "halyard.yaml",
        "halyard.db",
        "audit.jsonl",
        "last-said.json",
        "prompts/runs-advice.md",
        "logs/bridge-2026-W42.log",
    } <= names
    # Older copies hold old secrets, and the code is git's to keep.
    assert not any(name.endswith(".bak") or name.startswith("src/") for name in names)


def test_the_database_is_a_consistent_copy_while_it_is_in_use(tmp_path: Path) -> None:
    """Read with SQLite's own backup, so a service writing to it — rows still in
    its write-ahead log — still gives a whole copy."""
    with closing(a_machine(tmp_path)):
        target = exported(tmp_path)

    zipfile.ZipFile(target).extract("halyard.db", tmp_path / "back")
    with closing(sqlite3.connect(tmp_path / "back" / "halyard.db")) as copy:
        assert copy.execute("SELECT count(*) FROM runs").fetchone() == (3,)


def test_the_zip_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    """It holds the bot token."""
    with closing(a_machine(tmp_path)):
        target = exported(tmp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_machine_with_nothing_yet_still_gets_a_zip(tmp_path: Path) -> None:
    target = exported(tmp_path)

    assert zipfile.ZipFile(target).namelist() == []


def test_the_files_a_project_s_configuration_points_at_go_in_too(tmp_path: Path) -> None:
    """A transition's prompt, an inspection: they live in the project, and are
    as much this setup as the YAML that names them."""
    project = tmp_path / "alpha"
    (project / "NOTES" / "halyard" / "handoffs").mkdir(parents=True)
    (project / "NOTES" / "halyard" / "handoffs" / "developed.md").write_text("# developed\n")
    (project / "NOTES" / "proof.md").write_text("# proof\n")
    halyard = tmp_path / "halyard"
    halyard.mkdir()
    (halyard / "halyard.yaml").write_text(
        f"""\
projects:
  alpha:
    path: {project}
    inspections:
      proof: NOTES/proof.md
    transitions:
      developed:
        prompt: NOTES/halyard/handoffs/developed.md
        followup_prompt: NOTES/halyard/handoffs/gone.md
    agents:
      nav: {{runtime: claude-code}}
"""
    )

    names = set(zipfile.ZipFile(exported(halyard)).namelist())

    assert "project-files/alpha/NOTES/halyard/handoffs/developed.md" in names
    assert "project-files/alpha/NOTES/proof.md" in names
    assert not any("gone.md" in name for name in names)


def test_a_configuration_that_does_not_read_still_backs_up_the_rest(tmp_path: Path) -> None:
    (tmp_path / "halyard.yaml").write_text("projects: [not, a, mapping\n")
    (tmp_path / "audit.jsonl").write_text("{}\n")

    names = set(zipfile.ZipFile(exported(tmp_path)).namelist())

    assert {"halyard.yaml", "audit.jsonl"} <= names
