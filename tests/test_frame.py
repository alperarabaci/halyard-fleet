"""Tests for `halyard.frame` — where the work stands, as Halyard reads it."""

from __future__ import annotations

import os
import shutil
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


def test_the_fingerprint_is_of_the_files_not_the_commits(tmp_path: Path) -> None:
    """The same files give the same fingerprint however much of them is
    committed: a branch is squash-merged, and its commits are not a reference
    that lasts."""
    committed(tmp_path)
    (tmp_path / "a.txt").write_text("b\n")
    uncommitted = frame.tree(tmp_path)
    git(tmp_path, "commit", "-qam", "second")
    after = frame.tree(tmp_path)

    assert uncommitted is not None and after is not None
    assert uncommitted.content == after.content
    assert uncommitted.head != after.head
    assert after.clean and not uncommitted.clean


def test_any_edit_moves_the_fingerprint_and_an_ignored_file_does_not(tmp_path: Path) -> None:
    committed(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore builds")
    before = frame.tree(tmp_path)
    assert before is not None

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_text("built\n")
    ignored = frame.tree(tmp_path)
    (tmp_path / "notes.md").write_text("new\n")
    untracked = frame.tree(tmp_path)

    assert ignored is not None and ignored.content == before.content
    assert untracked is not None and untracked.content != before.content


def test_the_context_says_which_machine_and_which_files(tmp_path: Path) -> None:
    """The same repository sits on more than one machine, and a report from
    one is not about the files on another."""
    committed(tmp_path)
    now = frame.tree(tmp_path)
    assert now is not None

    said = frame.context(tmp_path, "alpha-engine")

    assert f"Host: {frame.host()}" in said
    assert f"Content: {now.content} (git write-tree, clean)" in said


def test_the_context_says_whether_these_are_the_files_the_reply_was_about(
    tmp_path: Path,
) -> None:
    """Kept as the reply came in, compared now. A check on a report three hours
    old, told nothing of it, set about rebuilding the tree."""
    committed(tmp_path)
    then = frame.tree(tmp_path)
    assert then is not None

    same = frame.context(tmp_path, "alpha-engine", replied="17:14", reply=then)
    (tmp_path / "a.txt").write_text("b\n")
    moved = frame.context(tmp_path, "alpha-engine", replied="17:14", reply=then)
    unknown = frame.context(tmp_path, "alpha-engine", replied="17:14")

    stood = f"At the reply: 17:14 · HEAD {then.head} · Content {then.content}"
    assert f"{stood} · same files" in same
    assert f"{stood} · files changed since" in moved
    assert "At the reply: 17:14 · not recorded" in unknown


def test_reading_where_the_tree_stands_never_writes_to_it(tmp_path: Path) -> None:
    """A commit made from the phone at that moment must not find the index
    locked, so git is not let refresh it on the way."""
    committed(tmp_path)
    index = tmp_path / ".git" / "index"
    before = index.stat().st_mtime_ns
    later = before + 5_000_000_000
    os.utime(tmp_path / "a.txt", ns=(later, later))

    frame.context(tmp_path, "alpha-engine")

    assert index.stat().st_mtime_ns == before


def test_the_envelope_is_a_list_with_each_facts_name_first() -> None:
    assert frame.envelope(["Project: alpha-engine", "Host: mini"]) == [
        "Envelope:",
        "- Project: alpha-engine",
        "- Host: mini",
    ]


def test_the_tasks_own_labels_sit_under_the_work_item(tmp_path: Path) -> None:
    """What the task is, together: its number, then what it is labelled."""
    committed(tmp_path, "359-organization-rollout")

    said = frame.context(tmp_path, "alpha-engine", labels={"level": "level::3"})

    at = said.index("Work item: alpha-engine#359")
    assert said[at + 1] == "level: level::3"


def test_content_is_the_tree_anybody_can_compute_with_git(tmp_path: Path) -> None:
    """The steps a team can put in its own documents give the same id — so a
    reviewer can tie it to a tree, and a clean tree's is HEAD's own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    committed(repo)
    clean = frame.tree(repo)
    (repo / "a.txt").write_text("edited\n")
    (repo / "new.md").write_text("untracked\n")
    edited = frame.tree(repo)

    by_hand = tmp_path / "index"
    shutil.copyfile(repo / ".git" / "index", by_hand)
    separate = {**os.environ, "GIT_INDEX_FILE": str(by_hand)}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=separate, check=True)
    written = subprocess.run(
        ["git", "-C", str(repo), "write-tree"],
        env=separate,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert clean is not None and edited is not None
    assert clean.content == git(repo, "rev-parse", "HEAD^{tree}").strip()[:12]
    assert clean.clean
    assert edited.content == written[:12]
    assert not edited.clean


def test_uncommitted_counts_the_new_files_too(tmp_path: Path) -> None:
    """Git's summary leaves untracked files out, and `nothing` beside three new
    files read as no change at all — while `Content` already held them."""
    committed(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore builds")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_text("built\n")
    clean = frame.context(tmp_path, "alpha-engine")

    (tmp_path / "notes.md").write_text("new\n")
    (tmp_path / "plan.md").write_text("new\n")
    new = frame.context(tmp_path, "alpha-engine")
    (tmp_path / "a.txt").write_text("b\n")
    both = frame.context(tmp_path, "alpha-engine")

    assert "Uncommitted: nothing" in clean
    assert "Uncommitted: 2 untracked files" in new
    changed = "1 file changed, 1 insertion(+), 1 deletion(-)"
    assert f"Uncommitted: {changed} · 2 untracked files" in both
