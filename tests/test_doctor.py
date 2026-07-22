"""What `halyard sessions` prints, and where it gets it from.

One mistake has now appeared twice in this codebase: recovering a directory by
decoding the name of the directory the transcripts sit in. That name is a lossy
encoding — every separator became a dash — so `halyard-fleet` and
`halyard/fleet` encode identically and nothing distinguishes them afterwards.

It was fixed in session lookup and left in this listing, which is the output the
README tells people to copy into `.env`.
"""

from __future__ import annotations

import json
from pathlib import Path

from halyard import doctor


def transcript(root: Path, encoded: str, *, name: str, cwd: str, chosen: bool = True) -> Path:
    """A transcript where the recorded cwd disagrees with the directory name."""
    directory = root / encoded
    directory.mkdir(parents=True)
    path = directory / "abc123.jsonl"
    path.write_text(
        json.dumps({"type": "user", "cwd": cwd, "sessionId": "abc123"})
        + "\n"
        + json.dumps(
            {"type": "custom-title", "customTitle": name}
            if chosen
            else {"type": "ai-title", "aiTitle": name}
        )
        + "\n"
    )
    return path


def test_the_listed_directory_comes_from_the_transcript(tmp_path, monkeypatch, capsys) -> None:
    """A dash in a directory name must survive being listed.

    `-Users-me-code-halyard-fleet` is what a session in `.../code/halyard-fleet`
    is filed under. Turning the dashes back into separators produces
    `.../code/halyard/fleet`, which is a directory that does not exist — printed
    beside a name somebody is about to copy into a config file.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".claude" / "projects"
    transcript(
        root,
        "-Users-me-code-halyard-fleet",
        name="alpha-engine-driver",
        cwd="/Users/me/code/halyard-fleet",
    )

    assert doctor.sessions() == 0

    printed = capsys.readouterr().out
    assert "/Users/me/code/halyard-fleet" in printed
    assert "halyard/fleet" not in printed


def test_a_long_path_is_printed_whole(tmp_path, monkeypatch, capsys) -> None:
    """It used to be cut to the last 60 characters, mid-directory-name.

    Which produced things like `mmer/Documents/...` — a path that looks like a
    path and is not one, in a listing whose only job is to be copied.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    deep = "/Users/me/Documents/dev/ai/agent-platform/investment/alpha-engine"
    transcript(
        tmp_path / ".claude" / "projects",
        "-Users-me-Documents-dev-ai-agent-platform-investment-alpha-engine",
        name="alpha-engine-navigator",
        cwd=deep,
    )

    doctor.sessions()

    assert deep in capsys.readouterr().out


def test_a_session_with_no_recorded_directory_says_so(tmp_path, monkeypatch, capsys) -> None:
    """Silence would read as "no directory needed", which is not the case."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    transcript(tmp_path / ".claude" / "projects", "-Users-me-code-thing", name="seat", cwd="")

    doctor.sessions()

    assert "not recorded" in capsys.readouterr().out


def test_project_root_is_the_repository_root(tmp_path) -> None:
    """Measured: a session in a subdirectory is gated from the top of its repo."""
    (tmp_path / ".git").mkdir()
    inside = tmp_path / "web" / "src"
    inside.mkdir(parents=True)

    assert doctor.project_root(inside) == tmp_path


def test_without_a_repository_a_directory_stands_alone(tmp_path) -> None:
    """And measured the other way: with no `.git` above it, nothing inherits."""
    inside = tmp_path / "web" / "src"
    inside.mkdir(parents=True)

    assert doctor.project_root(inside) == inside


def test_a_name_claude_invented_is_marked(tmp_path, monkeypatch, capsys) -> None:
    """Only a name a person chose is stable.

    Claude rewrites a generated title as the conversation moves, so a seat
    pointed at one routes correctly the day it is copied and then stops without
    an error — which reads as Halyard losing messages rather than as a name
    having moved underneath it. This listing exists to be copied from, so it
    has to say which names are safe to copy.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".claude" / "projects"
    transcript(root, "-a", name="alpha-engine-driver", cwd="/a", chosen=True)
    transcript(root, "-b", name="Run echo hello command", cwd="/b", chosen=False)

    doctor.sessions()

    printed = capsys.readouterr().out
    chosen_line = next(ln for ln in printed.splitlines() if "alpha-engine-driver" in ln)
    invented_line = next(ln for ln in printed.splitlines() if "Run echo hello" in ln)
    assert "auto-titled" not in chosen_line
    assert "auto-titled" in invented_line
    assert "Rename the" in printed


def test_no_warning_when_every_name_was_chosen(tmp_path, monkeypatch, capsys) -> None:
    """A warning that is always present is one nobody reads."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    transcript(
        tmp_path / ".claude" / "projects",
        "-a",
        name="alpha-engine-driver",
        cwd="/a",
        chosen=True,
    )

    doctor.sessions()

    assert "auto-titled" not in capsys.readouterr().out
