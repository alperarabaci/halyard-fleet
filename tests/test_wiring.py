"""Adding and removing the gate without taking anything else with it.

The file being edited belongs to Claude Code as much as to Halyard. Every test
here is about that: what must survive a write, and what must not be removed by
somebody else's uninstall.
"""

from __future__ import annotations

import json
import os
import time
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


# --- a second runtime -------------------------------------------------------


CODEX = next(r for r in wiring.RUNTIMES if r.name == "codex")


def test_codex_hooks_go_in_their_own_file(tmp_path: Path) -> None:
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    assert written["hooks"]["PreToolUse"]
    assert not (project / ".claude").exists()


def test_codex_gets_its_own_matcher_dialect(tmp_path: Path) -> None:
    """`^Bash$` rather than the bare `Bash` Claude Code takes."""
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    assert written["hooks"]["PreToolUse"][0]["matcher"] == "^Bash$"


def test_the_command_is_absolute_because_codex_expands_nothing(tmp_path: Path) -> None:
    """Codex has no project-directory variable — only $CODEX_HOME.

    A file carrying `$CLAUDE_PROJECT_DIR` does not fail to load under Codex.
    The hook runs and dies looking for a directory by that literal name, which
    is what this repository's own `hook: Stop Failed` turned out to be.
    """
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    command = written["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith("/")
    assert "$" not in command


def test_unwiring_covers_a_runtime_whose_cli_is_gone(tmp_path: Path) -> None:
    """Removal is attempted everywhere, not only where a CLI is installed.

    A hook left behind after a CLI is uninstalled still points at a bridge, and
    the next person to install that CLI inherits a gate they never asked for.
    """
    project = repo(tmp_path)
    wiring.wire(project, runtimes=wiring.RUNTIMES)

    wiring.unwire(project)

    assert json.loads((project / ".codex" / "hooks.json").read_text()) == {}
    assert json.loads((project / ".claude" / "settings.local.json").read_text()) == {}


def test_a_hook_with_no_trust_record_is_reported(tmp_path: Path) -> None:
    """Codex skips an untrusted hook in silence — measured.

    The turn completes, nothing is printed, and a PreToolUse gate that is not
    trusted is not a gate. Absence of a record is the one thing that can be
    stated for certain, so it is the thing reported.
    """
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    empty = tmp_path / "config.toml"
    empty.write_text("")

    pending = wiring.codex_untrusted(hooks_file, empty)

    assert len(pending) == 2
    assert all(str(hooks_file) in key for key in pending)
    assert any(key.endswith(":pretooluse:0:0") for key in pending)


def test_a_recorded_hook_is_not_reported_as_untrusted(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    config = tmp_path / "config.toml"
    config.write_text(
        "".join(
            f'[hooks.state."{key}"]\ntrusted_hash = "sha256:x"\n'
            for key in wiring.codex_trust_keys(hooks_file)
        )
    )

    assert wiring.codex_untrusted(hooks_file, config) == []


def test_editing_the_hooks_file_makes_trust_stale(tmp_path: Path) -> None:
    """The dangerous reading is the other one.

    A trust key that still exists with an outdated hash looks exactly like a
    trusted hook. Codex disagrees and says nothing about it.
    """
    project = repo(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text("")
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    os.utime(hooks_file, (time.time() + 10, time.time() + 10))

    assert wiring.codex_trust_is_stale(hooks_file, config) is True


def test_trust_is_not_claimed_to_be_fresh(tmp_path: Path) -> None:
    """The inference only runs one way: a newer config proves nothing, because
    Codex rewrites it for unrelated reasons."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    config = tmp_path / "config.toml"
    config.write_text("")
    os.utime(config, (time.time() + 10, time.time() + 10))

    assert wiring.codex_trust_is_stale(hooks_file, config) is False
