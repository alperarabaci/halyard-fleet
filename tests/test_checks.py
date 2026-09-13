"""Tests for `halyard.checks` — reading a project's check, and what it is asked."""

from __future__ import annotations

import subprocess
from pathlib import Path

from halyard import checks


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def test_a_check_is_read_relative_to_its_project(tmp_path: Path) -> None:
    (tmp_path / "NOTES").mkdir()
    (tmp_path / "NOTES" / "proof.md").write_text("# proof\nLook for claims.\n")

    assert checks.read(Path("NOTES/proof.md"), tmp_path) == "# proof\nLook for claims."


def test_a_check_that_is_not_there_reads_as_nothing(tmp_path: Path) -> None:
    assert checks.read(Path("NOTES/gone.md"), tmp_path) == ""


def test_the_context_names_the_task_and_where_the_tree_stands(tmp_path: Path) -> None:
    """A check of a report is only good for the tree the report was about."""
    git(tmp_path, "init", "-q", "-b", "353-organization-rollout")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "a.txt").write_text("b\n")

    said = checks.context(tmp_path, "alpha-engine")

    assert "Work item: alpha-engine#353" in said
    assert any(line.startswith("HEAD: ") for line in said)
    assert any("1 file changed" in line for line in said)


def test_the_prompt_says_the_check_cannot_look_for_itself() -> None:
    """The turn runs apart from the project. Saying so keeps an answer that
    needed a command run from reading as though it had run one."""
    asked = checks.prompt(
        "# proof", context=["Project: alpha-engine"], note="delivery", text="42 passed"
    )

    assert asked.startswith("# proof")
    assert "cannot open files or run commands" in asked
    assert "delivery" in asked
    assert asked.endswith("42 passed")


def test_a_fenced_answer_loses_its_fence() -> None:
    assert checks.unfenced("```\nproof · no finding\n```") == "proof · no finding"
    assert checks.unfenced("proof · no finding") == "proof · no finding"
