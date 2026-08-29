"""Tests for committing what is already work.

Driven against real repositories rather than a mocked `subprocess`. What can
actually go wrong here is misreading git's own output — a rename arriving as
three fields, a binary file counted as `-`, a detached head that looks like a
branch — and a mock would only ever return what this file already believes.

The refusals get the most attention. Everything this declines to do is a thing
somebody would otherwise have to undo from a phone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from halyard import commits
from halyard.commits import repository


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on an issue-named branch with one commit behind it."""
    place = tmp_path / "alpha-engine"
    place.mkdir()
    git(place, "init", "-q", "-b", "281-power-gen-minor-fixes")
    git(place, "config", "user.email", "t@example.com")
    git(place, "config", "user.name", "Tester")
    (place / "seed.txt").write_text("a\n")
    git(place, "add", ".")
    git(place, "commit", "-qm", "alpha-engine#279 p2")
    return place


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)


# --- what the reference is, and is not -------------------------------------


def test_an_issue_branch_gives_the_reference_the_project_files_under() -> None:
    assert commits.reference_for("alpha-engine", "281-power-gen-minor-fixes") == "alpha-engine#281"


def test_a_branch_not_named_for_an_issue_gets_no_reference() -> None:
    """`feat/runtime-isolation` has no issue behind it, and inventing one would
    file the work under somebody else's."""
    assert commits.reference_for("halyard-fleet", "feat/runtime-isolation") is None
    assert commits.reference_for("halyard-fleet", "main") is None


def test_a_number_that_is_not_a_prefix_is_not_an_issue() -> None:
    assert commits.reference_for("p", "release-2026") is None
    assert commits.reference_for("p", "fix/282-thing") is None


# --- what is staged --------------------------------------------------------


def test_staged_changes_are_read_with_their_shape(repo: Path) -> None:
    stage(repo, "loader.py", "def load():\n    return 1\n")
    stage(repo, "seed.txt", "b\n")

    work = commits.read(repo, "alpha-engine")

    assert work.blocked is None
    assert work.branch == "281-power-gen-minor-fixes"
    assert work.reference == "alpha-engine#281"
    assert {(c.status, c.path) for c in work.changes} == {("A", "loader.py"), ("M", "seed.txt")}
    assert (work.insertions, work.deletions) == (3, 1)
    assert "def load()" in work.diff
    assert work.style == ("alpha-engine#279 p2",)


def test_a_rename_is_reported_under_the_name_it_has_now(repo: Path) -> None:
    """git writes a rename as `R100\told\tnew`, and taking the second field
    would name a file that no longer exists."""
    git(repo, "mv", "seed.txt", "planted.txt")

    work = commits.read(repo, "alpha-engine")

    assert [c.path for c in work.changes] == ["planted.txt"]
    assert work.changes[0].status == "R"


def test_a_binary_file_counts_as_something_rather_than_nothing(repo: Path) -> None:
    """numstat writes `-` for binary. Parsed naively that is a crash; parsed
    carelessly it is zero, and a commit of only images looks empty."""
    (repo / "logo.png").write_bytes(bytes(range(256)) * 8)
    git(repo, "add", "logo.png")

    work = commits.read(repo, "alpha-engine")

    assert [c.path for c in work.changes] == ["logo.png"]
    assert work.blocked is None


# --- what it refuses to do -------------------------------------------------


def test_a_file_nobody_staged_is_still_the_work(repo: Path) -> None:
    """The whole point. A navigator and a driver write code and stage nothing,
    and the first version of this answered "nothing is staged" to the person
    who asked it to commit an afternoon of their output."""
    (repo / "unwork.txt").write_text("written by an agent\n")

    work = commits.read(repo, "alpha-engine")

    assert work.blocked is None
    assert [c.path for c in work.changes] == ["unwork.txt"]
    assert work.changes[0].is_new
    assert "written by an agent" in work.diff


def test_a_clean_branch_is_the_only_nothing_to_commit(repo: Path) -> None:
    work = commits.read(repo, "alpha-engine")

    assert work.blocked is not None
    assert "Nothing has changed" in work.blocked
    assert work.changes == ()


def test_a_detached_head_is_refused(repo: Path) -> None:
    """A commit here belongs to no branch and is gone at the next checkout."""
    git(repo, "checkout", "-q", "--detach")
    stage(repo, "seed.txt", "b\n")

    assert "detached" in (commits.read(repo, "alpha-engine").blocked or "")


def test_a_half_finished_merge_is_refused(repo: Path) -> None:
    """Committing mid-merge finishes somebody else's commit under a message
    written for a different one."""
    stage(repo, "seed.txt", "b\n")
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))

    assert "a merge" in (commits.read(repo, "alpha-engine").blocked or "")


def test_a_rebase_in_progress_is_refused(repo: Path) -> None:
    stage(repo, "seed.txt", "b\n")
    (repo / ".git" / "rebase-merge").mkdir()

    assert "a rebase" in (commits.read(repo, "alpha-engine").blocked or "")


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    assert "not a git repository" in (commits.read(tmp_path, "whatever").blocked or "")


def test_a_repository_that_was_moved_away_is_refused(tmp_path: Path) -> None:
    """Said as its own sentence: a path that vanished is a configuration
    problem, and "not a git repository" would send somebody looking at git."""
    gone = tmp_path / "moved-somewhere-else"

    assert "not on this machine" in (commits.read(gone, "whatever").blocked or "")


# --- what the model is shown ----------------------------------------------


def test_a_lockfile_is_listed_but_its_diff_is_not_sent(repo: Path) -> None:
    """Thousands of lines that say nothing a message could use."""
    stage(repo, "uv.lock", "\n".join(f"line {n}" for n in range(500)))
    stage(repo, "loader.py", "def load():\n    return 1\n")

    work = commits.read(repo, "alpha-engine")

    assert {c.path for c in work.changes} == {"uv.lock", "loader.py"}
    assert "def load()" in work.diff
    assert "line 499" not in work.diff


def test_a_commit_of_only_lockfiles_still_works(repo: Path) -> None:
    """With nothing worth reading there is no diff, and the file list has to
    carry the message on its own rather than the whole thing failing."""
    stage(repo, "uv.lock", "version = 2\n")

    work = commits.read(repo, "alpha-engine")

    assert work.blocked is None
    assert work.diff == ""
    assert [c.path for c in work.changes] == ["uv.lock"]


def test_a_diff_too_large_to_send_is_cut_and_says_so(repo: Path) -> None:
    """Written into a tracked file, so the whole thing reaches `diff HEAD` —
    a new file is capped at `NEW_FILE_LINES` before it ever gets here."""
    (repo / "seed.txt").write_text("\n".join(f"x = {n}" for n in range(20_000)))

    work = commits.read(repo, "alpha-engine")

    assert work.truncated is True
    assert len(work.diff) == repository.DIFF_LIMIT
    assert "cut short" in commits.prompt(work)


def test_the_prompt_tells_the_model_not_to_write_the_reference(repo: Path) -> None:
    """Otherwise it writes it too, and the line arrives with it twice."""
    stage(repo, "loader.py", "x = 1\n")

    asked = commits.prompt(commits.read(repo, "alpha-engine"))

    assert "Do NOT write the issue reference" in asked
    assert "alpha-engine#281" in asked
    assert "alpha-engine#279 p2" in asked  # the house style, shown not described
    assert "loader.py" in asked


def test_a_prompt_without_a_reference_does_not_mention_one(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feat/runtime-isolation")
    stage(repo, "loader.py", "x = 1\n")

    asked = commits.prompt(commits.read(repo, "halyard-fleet"))

    assert "issue reference" not in asked


# --- turning what the model said into a message ----------------------------


def test_the_reference_is_put_in_front_of_what_the_model_wrote() -> None:
    assert commits.assemble("alpha-engine#281", "power gen fixes") == (
        "alpha-engine#281 power gen fixes"
    )


def test_a_model_that_wrote_the_reference_anyway_does_not_get_two() -> None:
    assert commits.assemble("alpha-engine#281", "alpha-engine#281 power gen fixes") == (
        "alpha-engine#281 power gen fixes"
    )


def test_a_fenced_or_padded_answer_is_taken_apart() -> None:
    assert commits.assemble("p#1", "```\nthe subject\n```") == "p#1 the subject"
    assert commits.assemble("p#1", '  "the subject"  ') == 'p#1 "the subject"'


def test_a_model_that_said_nothing_leaves_the_reference_standing() -> None:
    """`alpha-engine#282` on its own is a real commit in this history, so it is
    a poor message rather than an invalid one."""
    assert commits.assemble("alpha-engine#282", "   ") == "alpha-engine#282"


def test_without_a_reference_the_line_stands_alone() -> None:
    assert commits.assemble(None, "One shape for a listed session") == (
        "One shape for a listed session"
    )


# --- making it --------------------------------------------------------------


def test_the_commit_is_made_and_its_sha_comes_back(repo: Path) -> None:
    stage(repo, "loader.py", "x = 1\n")

    sha = commits.commit(repo, "alpha-engine#281 loader stub")

    assert git(repo, "log", "-1", "--format=%s").strip() == "alpha-engine#281 loader stub"
    assert git(repo, "rev-parse", "--short", "HEAD").strip() == sha


def test_a_message_full_of_shell_is_committed_as_text(repo: Path) -> None:
    """The message is written by a model and can be typed over by a person, and
    it reaches `git commit -m`. Through argv, where a backtick is a backtick."""
    hostile = 'fix `whoami`; rm -rf / && echo "$(id)"'
    stage(repo, "loader.py", "x = 1\n")

    commits.commit(repo, hostile)

    assert git(repo, "log", "-1", "--format=%s").strip() == hostile
    assert (repo / "loader.py").exists()


def test_a_commit_that_git_refuses_says_what_git_said(repo: Path) -> None:
    """Nothing staged, so `git commit` fails. The message it gives is more
    useful than anything this module could invent."""
    with pytest.raises(commits.GitError):
        commits.commit(repo, "nothing to see")
