"""Tests for `halyard.inspections.shares` — what part of a branch each inspection reads.

Seen on a branch of eleven commits: the delivery was committed before its
close, so the close's inspections were told three uncommitted lines and had
nothing of the delivery to read. Each now reads the branch since its base, and
its own share since it last read whole — in a real repository here, because the
point is what git says about it.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from halyard import frame, transitions
from halyard.core.config_file import Transition
from halyard.inspections import shares


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def a_branch(tmp_path: Path) -> Path:
    """A project on a branch two commits past `main`, with its inspections."""
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Tester")
    (repo / "NOTES").mkdir()
    (repo / "NOTES" / "proof.md").write_text("# proof")
    (repo / "NOTES" / "claims.md").write_text("# claims")
    (repo / "loader.py").write_text("rows = []\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "the base")
    git(repo, "switch", "-qc", "501-delivery")
    (repo / "port.py").write_text("class Port: ...\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "a port")
    (repo / "loader.py").write_text("rows = [1]\n")
    git(repo, "commit", "-qam", "a row")
    return repo


class Asking:
    """An `Asker` that answers from a script and keeps what each turn was asked."""

    def __init__(self, says: dict[str, str | None] | None = None) -> None:
        self.says = says or {}
        self.asked: dict[str, str] = {}

    async def ask(self, text: str, *, name: str | None = None, **_) -> str | None:
        inspection = (name or "").split(" · ")[0]
        self.asked[inspection] = text
        return self.says.get(inspection, f"{inspection}: no finding")


class Delivered:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def to_seat(self, label: str, text: str) -> None:
        self.sent.append(text)


CLOSE = Transition(name="close", inspections=("proof", "claims"))


async def close(repo: Path, kept: Path, asker: Asking, **more) -> str:
    """Take the close, and what reached the navigator."""
    delivery = Delivered()
    await transitions.take(
        CLOSE,
        project=repo,
        context=["Work item: alpha-engine#501"],
        note="",
        reply="Delivered: a port and a row.",
        arrived="15:47",
        sender="drv (driver)",
        recipient_label="nav",
        recipient="nav (navigator)",
        project_inspections={"proof": Path("NOTES/proof.md"), "claims": Path("NOTES/claims.md")},
        asker=asker,
        model="sonnet",
        timeout=5,
        delivery=delivery,
        shares=kept,
        **more,
    )
    [sent] = delivery.sent
    return sent


def line(text: str, starts: str) -> str:
    """The envelope line that starts so."""
    found = [each for each in text.splitlines() if each.startswith(f"- {starts}")]
    assert found, f"no line starting {starts!r} in:\n{text}"
    return found[0]


def short(ident: str) -> str:
    return ident[: frame.SHOWN]


async def test_every_inspection_is_told_the_transition_and_the_branch_since_its_base(
    tmp_path: Path,
) -> None:
    """As two commits, so it runs the same diff on any machine, fetched or not."""
    repo = a_branch(tmp_path)
    asker = Asking()

    await close(repo, tmp_path / "shares.json", asker)

    based = git(repo, "merge-base", "HEAD", "main")
    files = frame.tree(repo)
    for asked in asker.asked.values():
        assert "- Transition: close" in asked
        assert line(asked, "Base:") == (
            f"- Base: main at {short(based)} · 2 commits since · "
            "2 files changed, 2 insertions(+), 1 deletion(-) — "
            f"git diff {short(based)} {files.content}"
        )


async def test_a_base_that_moved_on_is_measured_from_where_the_branch_left_it(
    tmp_path: Path,
) -> None:
    """From its tip, what the base gained since would read as the branch
    undoing it."""
    repo = a_branch(tmp_path)
    left = git(repo, "merge-base", "HEAD", "main")
    git(repo, "switch", "-q", "main")
    (repo / "other.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "on main meanwhile")
    git(repo, "switch", "-q", "501-delivery")
    asker = Asking()

    await close(repo, tmp_path / "shares.json", asker)

    said = line(asker.asked["proof"], "Base:")
    assert said.startswith(f"- Base: main at {short(left)} · 2 commits since · 2 files changed")


async def test_a_first_run_here_is_told_its_share_is_the_whole_branch(tmp_path: Path) -> None:
    repo = a_branch(tmp_path)
    asker = Asking()

    await close(repo, tmp_path / "shares.json", asker)

    assert line(asker.asked["proof"], "Since this inspection") == (
        "- Since this inspection last ran here: it has not, on this branch on this "
        "machine — its share is the base's range"
    )
    assert "last answer here" not in asker.asked["proof"]


async def test_the_next_run_reads_what_changed_since_and_its_own_last_answer(
    tmp_path: Path,
) -> None:
    """What it found stays with it: an open finding or a range it could not
    measure is the inspection's to carry, and Halyard reads none of it."""
    repo = a_branch(tmp_path)
    kept = tmp_path / "shares.json"
    first = frame.tree(repo)
    await close(repo, kept, Asking({"proof": "candidate: port.py:1 has no test"}))
    (repo / "port.py").write_text("class Port:\n    rows = 1\n")
    asker = Asking()

    await close(repo, kept, asker)

    based = short(git(repo, "merge-base", "HEAD", "main"))
    now = frame.tree(repo)
    assert re.fullmatch(
        r"- Since this inspection last ran here: \w{3} \d+ \d\d:\d\d · "
        r"1 file changed, 2 insertions\(\+\), 1 deletion\(-\) — "
        rf"git diff {first.content} {now.content}",
        line(asker.asked["proof"], "Since this inspection"),
    )
    assert re.search(
        rf"This inspection's last answer here, at \w{{3}} \d+ \d\d:\d\d, on git diff {based} "
        rf"{first.content}:\n\ncandidate: port.py:1 has no test\n",
        asker.asked["proof"],
    )
    assert "claims: no finding" in asker.asked["claims"]
    assert "candidate" not in asker.asked["claims"], "each is handed its own"


async def test_work_inspected_before_it_was_committed_is_not_its_share_again(
    tmp_path: Path,
) -> None:
    """The mark is the files the inspection was given, not a commit: committing
    them afterwards changes nothing it has to read."""
    repo = a_branch(tmp_path)
    kept = tmp_path / "shares.json"
    (repo / "port.py").write_text("class Port:\n    rows = 1\n")
    await close(repo, kept, Asking())
    git(repo, "commit", "-qam", "the same files, committed")
    asker = Asking()

    await close(repo, kept, asker)

    assert " · nothing — git diff " in line(asker.asked["proof"], "Since this inspection")


async def test_an_inspection_that_did_not_read_whole_keeps_its_share(tmp_path: Path) -> None:
    """Its model said nothing: its mark stays, and the next share is wider —
    while the one that did read moves on."""
    repo = a_branch(tmp_path)
    kept = tmp_path / "shares.json"
    first = frame.tree(repo)
    await close(repo, kept, Asking())
    (repo / "port.py").write_text("class Port:\n    rows = 1\n")
    second = frame.tree(repo)
    await close(repo, kept, Asking({"claims": None}))
    (repo / "loader.py").write_text("rows = [1, 2]\n")
    asker = Asking()

    await close(repo, kept, asker)

    assert f"git diff {second.content} " in line(asker.asked["proof"], "Since this inspection")
    assert f"git diff {first.content} " in line(asker.asked["claims"], "Since this inspection")
    assert ", on git diff " in asker.asked["claims"], "its last answer is the one it gave whole"


async def test_the_navigator_reads_which_range_each_inspection_was_given(tmp_path: Path) -> None:
    repo = a_branch(tmp_path)
    based = short(git(repo, "merge-base", "HEAD", "main"))

    sent = await close(repo, tmp_path / "shares.json", Asking())

    files = frame.tree(repo)
    version = frame.version(Path("NOTES/proof.md"), repo)
    assert re.search(
        rf"\nInspection proof — NOTES/proof\.md @ {version} · complete · [^\n]*"
        rf" · on git diff {based} {files.content}:\n",
        sent,
    )


async def test_a_mark_whose_files_are_gone_counts_from_its_commit(tmp_path: Path) -> None:
    """A tree no commit holds is cleared by git after two weeks: the commit
    under it stands in, and the line says so."""
    repo = a_branch(tmp_path)
    kept = tmp_path / "shares.json"
    head = git(repo, "rev-parse", "HEAD")
    shares.keep(
        kept,
        "501-delivery",
        "close",
        "proof",
        shares.Mark(
            at=datetime.now(UTC),
            head=head,
            tree="0" * 40,
            base="",
            given="",
            answer="no finding",
        ),
    )
    asker = Asking()

    await close(repo, kept, asker)

    said = line(asker.asked["proof"], "Since this inspection")
    assert ", whose files are gone, so from its HEAD then · " in said
    assert f"git diff {short(head)} " in said


async def test_a_base_merged_in_since_is_said(tmp_path: Path) -> None:
    """Its changes are in the share then, and the reader is told whose they are."""
    repo = a_branch(tmp_path)
    kept = tmp_path / "shares.json"
    await close(repo, kept, Asking())
    git(repo, "switch", "-q", "main")
    (repo / "other.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "on main meanwhile")
    git(repo, "switch", "-q", "501-delivery")
    git(repo, "merge", "-q", "--no-edit", "main")
    asker = Asking()

    await close(repo, kept, asker)

    assert (
        " · the base was merged in since, so this includes the base's own changes — git diff "
        in line(asker.asked["proof"], "Since this inspection")
    )


async def test_the_remote_default_branch_is_the_base(tmp_path: Path) -> None:
    repo = a_branch(tmp_path)
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "fetch", "-q", "origin")
    git(repo, "remote", "set-head", "origin", "main")
    asker = Asking()

    await close(repo, tmp_path / "shares.json", asker)

    assert line(asker.asked["proof"], "Base:").startswith("- Base: origin/main at ")


async def test_a_project_s_own_base_wins(tmp_path: Path) -> None:
    repo = a_branch(tmp_path)
    git(repo, "branch", "release", "HEAD~1")
    asker = Asking()

    await close(repo, tmp_path / "shares.json", asker, base="release")

    assert line(asker.asked["proof"], "Base:").startswith(
        "- Base: release at " + short(git(repo, "rev-parse", "HEAD~1")) + " · 1 commit since · "
    )


def test_marks_are_kept_per_branch_transition_and_inspection_and_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    kept = tmp_path / "shares.json"
    monkeypatch.setattr(shares, "MARKS", 2)

    for minute, name in enumerate(("proof", "claims", "destructive")):
        shares.keep(
            kept,
            "501-delivery",
            "close",
            name,
            shares.Mark(
                at=datetime(2026, 11, 12, 15, minute, tzinfo=UTC),
                head="h",
                tree="t",
                base="",
                given="",
                answer="x" * (shares.ANSWER_LIMIT + 10),
            ),
        )

    assert sorted(json.loads(kept.read_text())) == [
        "501-delivery|close|claims",
        "501-delivery|close|destructive",
    ]
    mark = shares.last(kept, "501-delivery", "close", "destructive")
    assert mark is not None and len(mark.answer) == shares.ANSWER_LIMIT
    assert shares.last(kept, "501-delivery", "review", "destructive") is None
