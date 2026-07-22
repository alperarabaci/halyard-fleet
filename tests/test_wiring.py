"""Adding and removing the gate without taking anything else with it.

The file being edited belongs to Claude Code as much as to Halyard. Every test
here is about that: what must survive a write, and what must not be removed by
somebody else's uninstall.
"""

from __future__ import annotations

import json
from pathlib import Path

from halyard import wiring


def repo(tmp_path: Path, settings: dict | None = None) -> Path:
    """A project that looks like a checkout, optionally already configured."""
    (tmp_path / ".git").mkdir()
    if settings is not None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.local.json").write_text(json.dumps(settings, indent=2))
    return tmp_path


def read(path: Path) -> dict:
    return json.loads((path / ".claude" / "settings.local.json").read_text())


def test_wiring_keeps_the_permission_list(tmp_path: Path) -> None:
    """The failure this module exists to prevent.

    Losing `permissions.allow` produces no error and no obvious symptom — the
    session simply starts asking again about commands it settled months ago,
    and nobody connects that to a config edit from days earlier.
    """
    project = repo(tmp_path, {"permissions": {"allow": ["Bash(uv run *)", "WebSearch"]}})

    wiring.wire(project)

    assert read(project)["permissions"]["allow"] == ["Bash(uv run *)", "WebSearch"]
    assert read(project)["hooks"]["PreToolUse"]


def test_wiring_keeps_a_backup(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["WebSearch"]}})

    wiring.wire(project)

    backups = list((project / ".claude").glob("settings.local.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["permissions"]["allow"] == ["WebSearch"]


def test_wiring_an_untouched_project_creates_the_file(tmp_path: Path) -> None:
    project = repo(tmp_path)

    assert wiring.wire(project) == 0

    events = read(project)["hooks"]
    assert "PreToolUse" in events
    assert "Stop" in events


def test_wiring_twice_does_not_duplicate_the_hook(tmp_path: Path) -> None:
    """A hook listed twice would ask twice for one command."""
    project = repo(tmp_path)

    wiring.wire(project)
    wiring.wire(project)

    groups = read(project)["hooks"]["PreToolUse"]
    ours = [g for g in groups if any(wiring._is_ours(h["command"]) for h in g["hooks"])]
    assert len(ours) == 1


def test_unwiring_leaves_somebody_elses_hook_alone(tmp_path: Path) -> None:
    """Removal is by path, so this cannot uninstall a tool it did not install."""
    project = repo(
        tmp_path,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/somebody/elses/hook.sh"}],
                    }
                ]
            }
        },
    )
    wiring.wire(project)

    wiring.unwire(project)

    remaining = read(project)["hooks"]["PreToolUse"]
    assert len(remaining) == 1
    assert remaining[0]["hooks"][0]["command"] == "/somebody/elses/hook.sh"


def test_unwiring_keeps_the_permission_list(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["Bash(uv run *)"]}})
    wiring.wire(project)

    wiring.unwire(project)

    written = read(project)
    assert written["permissions"]["allow"] == ["Bash(uv run *)"]
    assert "hooks" not in written


def test_unwiring_what_was_never_wired_changes_nothing(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["WebSearch"]}})

    assert wiring.unwire(project) == 0

    assert read(project) == {"permissions": {"allow": ["WebSearch"]}}
    assert not list((project / ".claude").glob("*.bak"))


def test_a_subdirectory_is_wired_at_the_repository_root(tmp_path: Path) -> None:
    """Where Claude Code actually looks — measured, not assumed.

    A session opened under a monorepo's web app is gated by the `.claude/` at
    the top of the repository. Writing a second one next to the session would
    gate nothing while looking like it had.
    """
    project = repo(tmp_path)
    inside = project / "web" / "src"
    inside.mkdir(parents=True)

    wiring.wire(inside)

    assert (project / ".claude" / "settings.local.json").exists()
    assert not (inside / ".claude").exists()


def test_a_directory_outside_a_repository_is_wired_where_it_stands(tmp_path: Path) -> None:
    """With no `.git` above it, a parent's hooks never fire — so do not go up."""
    loose = tmp_path / "not-a-repo"
    loose.mkdir()

    wiring.wire(loose)

    assert (loose / ".claude" / "settings.local.json").exists()


def test_a_broken_settings_file_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    """Overwriting unreadable JSON would destroy whatever it was meant to hold."""
    project = repo(tmp_path)
    broken = project / ".claude" / "settings.local.json"
    broken.parent.mkdir(exist_ok=True)
    broken.write_text("{ this is not json")

    try:
        wiring.wire(project)
    except SystemExit as stop:
        assert "not valid JSON" in str(stop)
    else:
        raise AssertionError("wiring should refuse a file it cannot parse")

    assert broken.read_text() == "{ this is not json"
