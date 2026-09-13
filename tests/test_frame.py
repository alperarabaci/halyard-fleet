"""Tests for `halyard.frame` — where the work stands, as Halyard reads it."""

from __future__ import annotations

import subprocess
from pathlib import Path

from halyard import frame


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def committed(repo: Path, branch: str = "main") -> None:
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Tester")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")


def test_a_project_file_is_read_relative_to_its_project(tmp_path: Path) -> None:
    (tmp_path / "NOTES").mkdir()
    (tmp_path / "NOTES" / "proof.md").write_text("# proof\nLook for claims.\n")

    assert frame.read(Path("NOTES/proof.md"), tmp_path) == "# proof\nLook for claims."


def test_a_file_that_is_not_there_reads_as_nothing(tmp_path: Path) -> None:
    assert frame.read(Path("NOTES/gone.md"), tmp_path) == ""


def test_the_context_names_the_task_and_where_the_tree_stands(tmp_path: Path) -> None:
    """A judgement of a report is only good for the tree the report was about."""
    committed(tmp_path, "353-organization-rollout")
    (tmp_path / "a.txt").write_text("b\n")

    said = frame.context(tmp_path, "alpha-engine")

    assert "Work item: alpha-engine#353" in said
    assert any(line.startswith("HEAD: ") for line in said)
    assert any("1 file changed" in line for line in said)


def test_the_version_is_the_commit_that_last_changed_the_file(tmp_path: Path) -> None:
    """What a finding was a finding by, once the file changes next week."""
    committed(tmp_path)
    commit = git(tmp_path, "log", "-1", "--format=%h").strip()

    assert frame.version(Path("a.txt"), tmp_path) == commit
    (tmp_path / "a.txt").write_text("edited\n")
    assert frame.version(Path("a.txt"), tmp_path) == f"{commit} + local edits"
