"""Tests for `halyard rules` — what each project trusts to run without asking,
read from both places it is written and changed only in the database."""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard import rules_cli
from halyard.core.config_file import projects_from_yaml

CONFIG = """
projects:
  alpha-engine:
    path: /x
    runs:
      - make test-fast
      - bash *
  other:
    path: /y
"""


def machine(monkeypatch: pytest.MonkeyPatch, database: Path, *, switched: bool = True) -> None:
    """A machine of its own: this database, this configuration, never the
    service's."""
    described = {project.name: project for project in projects_from_yaml(CONFIG)}
    monkeypatch.setattr(rules_cli, "_database", lambda: database)
    monkeypatch.setattr(rules_cli, "_projects", lambda: described)
    monkeypatch.setattr(rules_cli, "_switched_on", lambda: switched)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "halyard.db"
    machine(monkeypatch, path)
    return path


def test_what_is_trusted_is_listed_with_where_it_is_written(database: Path, capsys) -> None:
    assert rules_cli.main(["add", "alpha-engine", "uv run pytest *"]) == 0
    capsys.readouterr()

    assert rules_cli.main([]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "alpha-engine"
    assert lines[1].split() == ["make", "test-fast", "halyard.yaml"]
    assert lines[2].split() == ["uv", "run", "pytest", "*", "halyard", "rules"]
    assert lines[3].split()[:3] == ["bash", "*", "ignored:"]


def test_a_list_that_is_switched_off_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    machine(monkeypatch, tmp_path / "halyard.db", switched=False)

    rules_cli.main(["list"])

    assert "none of these apply yet" in capsys.readouterr().out


def test_an_entry_that_could_run_anything_is_not_added(database: Path, capsys) -> None:
    assert rules_cli.main(["add", "alpha-engine", "uv run *"]) == 2
    assert "not added" in capsys.readouterr().err


def test_a_project_the_configuration_does_not_name_is_refused(database: Path, capsys) -> None:
    """A typo would be a list that never applies anywhere."""
    assert rules_cli.main(["add", "alpha", "make x"]) == 2
    assert "known: alpha-engine, other" in capsys.readouterr().err


def test_one_written_in_halyard_yaml_is_removed_there(database: Path, capsys) -> None:
    """The file is somebody's to edit; this changes only the database."""
    assert rules_cli.main(["remove", "alpha-engine", "make test-fast"]) == 1
    assert "remove it there" in capsys.readouterr().err


def test_one_added_here_is_removed_here(database: Path, capsys) -> None:
    rules_cli.main(["add", "alpha-engine", "npm run test"])

    assert rules_cli.main(["remove", "alpha-engine", "npm run test"]) == 0
    assert rules_cli.main(["remove", "alpha-engine", "npm run test"]) == 1


def test_a_list_goes_to_another_machine_and_back(
    database: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Both places at once, and nothing that was refused."""
    rules_cli.main(["add", "alpha-engine", "uv run pytest *"])
    exported = tmp_path / "rules.yaml"

    assert rules_cli.main(["export", str(exported)]) == 0
    text = exported.read_text(encoding="utf-8")
    assert "make test-fast" in text and "uv run pytest *" in text and "bash" not in text

    machine(monkeypatch, tmp_path / "other.db")
    capsys.readouterr()
    assert rules_cli.main(["import", str(exported)]) == 0

    assert capsys.readouterr().out.splitlines()[0] == "Added 1, already trusted 1, refused 0"


def test_an_import_refuses_what_it_cannot_take(database: Path, tmp_path: Path, capsys) -> None:
    given = tmp_path / "given.yaml"
    given.write_text("runs:\n  alpha-engine: [make lint, uv run *]\n  gone: [make x]\n")

    assert rules_cli.main(["import", str(given)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Added 1, already trusted 0, refused 2"
    assert any("could run anything" in line for line in lines)
    assert any("no project 'gone'" in line for line in lines)


@pytest.mark.parametrize(
    "args", [["add", "alpha-engine"], ["import"], ["drop"], ["list", "a", "b"]]
)
def test_what_it_does_not_take_is_refused_with_how_to_ask(database: Path, capsys, args) -> None:
    assert rules_cli.main(args) == 2
    assert "usage: halyard rules" in capsys.readouterr().err
