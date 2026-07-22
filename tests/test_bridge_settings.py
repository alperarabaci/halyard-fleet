"""Tests for how a bridge finds the control plane.

This is load-bearing for a fail-closed path. A bridge that resolves the wrong
address does not fail loudly — it denies every command with a message about a
port, which is indistinguishable from the system working correctly and
refusing you.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO / "bridge"

sys.path.insert(0, str(BRIDGE_DIR))
import _settings  # noqa: E402


@pytest.fixture
def config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the lookup at files a test controls, not the developer's own."""
    first = tmp_path / "project.env"
    second = tmp_path / "home.config"
    monkeypatch.setattr(_settings, "_CONFIG_FILES", (first, second))
    monkeypatch.delenv("HALYARD_URL", raising=False)
    return first, second


def test_the_environment_wins(config_files, monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = config_files
    first.write_text("HALYARD_URL=http://from-file:1\n")
    monkeypatch.setenv("HALYARD_URL", "http://from-env:2")

    # An explicit export is still an override, for anyone who wants one.
    assert _settings.control_plane_url() == "http://from-env:2"


def test_a_file_is_used_when_nothing_is_exported(config_files) -> None:
    first, _ = config_files
    first.write_text("# a comment\nHALYARD_URL=http://127.0.0.1:8799\nOTHER=x\n")

    # The point of the whole exercise: no export, and it still finds the address
    # the control plane was configured with.
    assert _settings.control_plane_url() == "http://127.0.0.1:8799"


def test_the_first_file_wins(config_files) -> None:
    first, second = config_files
    first.write_text("HALYARD_URL=http://first:1\n")
    second.write_text("HALYARD_URL=http://second:2\n")

    assert _settings.control_plane_url() == "http://first:1"


def test_a_later_file_is_used_when_the_first_says_nothing(config_files) -> None:
    first, second = config_files
    first.write_text("SOMETHING_ELSE=1\n")
    second.write_text("HALYARD_URL=http://second:2\n")

    assert _settings.control_plane_url() == "http://second:2"


@pytest.mark.parametrize(
    "line",
    ['HALYARD_URL="http://q:1"', "HALYARD_URL='http://q:1'", "HALYARD_URL=  http://q:1  "],
)
def test_quotes_and_padding_are_stripped(config_files, line: str) -> None:
    first, _ = config_files
    first.write_text(line + "\n")

    assert _settings.control_plane_url() == "http://q:1"


@pytest.mark.parametrize(
    "content", ["", "\n\n", "# only a comment\n", "HALYARD_URL=\n", "no equals sign here\n"]
)
def test_an_unhelpful_file_falls_back_to_the_default(config_files, content: str) -> None:
    first, _ = config_files
    first.write_text(content)

    assert _settings.control_plane_url() == _settings.DEFAULT_URL


def test_a_missing_file_is_not_an_error(config_files) -> None:
    # Reading configuration must never be the thing that breaks a bridge.
    assert _settings.control_plane_url() == _settings.DEFAULT_URL


def test_a_directory_where_a_file_was_expected_is_not_an_error(
    config_files, tmp_path: Path
) -> None:
    first, _ = config_files
    first.mkdir()

    assert _settings.control_plane_url() == _settings.DEFAULT_URL


def test_a_bad_timeout_falls_back_rather_than_raising(config_files) -> None:
    first, _ = config_files
    first.write_text("HALYARD_BRIDGE_TIMEOUT_SECONDS=not-a-number\n")

    assert _settings.timeout("HALYARD_BRIDGE_TIMEOUT_SECONDS", 330.0) == 330.0


def test_the_lookup_works_when_the_bridge_runs_from_an_unrelated_directory(
    tmp_path: Path,
) -> None:
    # Hooks run with whatever working directory Claude Code had. The import has
    # to resolve from the script's own location, not the caller's.
    result = subprocess.run(
        [sys.executable, str(BRIDGE_DIR / "relay.py")],
        input='{"session_id":"s","last_assistant_message":"hi"}',
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HALYARD_URL": "http://127.0.0.1:1"},
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stderr == ""


# --- doctor -----------------------------------------------------------------


def test_doctor_reports_a_problem_when_nothing_is_listening(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from halyard.doctor import run

    monkeypatch.setenv("HALYARD_CHANNEL", "stub_deny")
    monkeypatch.setenv("HALYARD_URL", "http://127.0.0.1:1")

    exit_code = run()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "nothing answering" in output
    # The whole point is that it says where the setting came from, because
    # "unreachable at 8787" is useless on its own.
    assert "HALYARD_URL, set in the environment" in output


# --- one key, derived ---------------------------------------------------------


@pytest.mark.parametrize(
    ("bind", "expected"),
    [
        ("127.0.0.1:8799", "http://127.0.0.1:8799"),
        ("127.0.0.1:8787", "http://127.0.0.1:8787"),
        ("192.168.1.5:9000", "http://192.168.1.5:9000"),
        # A server on every interface is still reached over loopback, and a
        # bridge told to connect to 0.0.0.0 is a bridge about to deny everything.
        ("0.0.0.0:8787", "http://127.0.0.1:8787"),
        ("::1:8787", "http://::1:8787"),
    ],
)
def test_the_url_is_derived_from_the_bind_address(config_files, bind: str, expected: str) -> None:
    first, _ = config_files
    first.write_text(f"HALYARD_BIND={bind}\n")

    # One key decides where the service listens, where compose publishes it, and
    # where the bridges look. Three keys kept in agreement by hand was three
    # chances to deny every command with a message about a port.
    assert _settings.control_plane_url() == expected


@pytest.mark.parametrize("bind", ["garbage", "127.0.0.1:", "", "127.0.0.1:notaport"])
def test_an_unusable_bind_falls_back_rather_than_producing_nonsense(
    config_files, bind: str
) -> None:
    first, _ = config_files
    first.write_text(f"HALYARD_BIND={bind}\n")

    assert _settings.control_plane_url() == _settings.DEFAULT_URL


def test_an_explicit_url_still_wins_over_the_derivation(config_files) -> None:
    first, _ = config_files
    first.write_text("HALYARD_BIND=127.0.0.1:8799\nHALYARD_URL=http://100.64.0.2:8787\n")

    # The derivation cannot cover a control plane on another machine reached
    # over Tailscale, so the override stays.
    assert _settings.control_plane_url() == "http://100.64.0.2:8787"


def test_doctor_says_where_the_address_came_from(
    config_files, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from halyard.doctor import run

    first, _ = config_files
    first.write_text("HALYARD_BIND=127.0.0.1:1\n")
    monkeypatch.setenv("HALYARD_CHANNEL", "stub_deny")

    run()

    output = capsys.readouterr().out
    assert "derived from HALYARD_BIND" in output


# --- the command line ---------------------------------------------------------


@pytest.mark.parametrize("argv", [["halyard", "doctor."], ["halyard", "docter"], ["halyard", "-h"]])
def test_a_mistyped_command_does_not_start_a_server(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import halyard.__main__ as entry

    started = []
    monkeypatch.setattr(entry, "serve", lambda: started.append(True))
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exit_info:
        entry.main()

    # `halyard doctor.` once bound a port and connected to Telegram when it was
    # asked to run a read-only check. A typo gets a usage message.
    assert exit_info.value.code == 2
    assert started == []
    assert "unknown command" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["halyard"], ["halyard", "serve"]])
def test_serving_is_the_default_and_can_be_named(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import halyard.__main__ as entry

    started = []
    monkeypatch.setattr(entry, "serve", lambda: started.append(True))
    monkeypatch.setattr(sys, "argv", argv)

    entry.main()

    assert started == [True]


# --- checking the projects the hooks are wired into ---------------------------
#
# This is the check that would have caught an afternoon: settings copied from
# one machine to another still named the first machine's paths, so the wrapper
# denied every command and the control plane never heard about any of it.


def gated_project(tmp_path: Path, hooks: dict, *, bridge_dir: Path | None = None) -> Path:
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"hooks": hooks}), encoding="utf-8"
    )
    return project


def a_session(project: Path, *, started: datetime | None = None):
    from halyard.agents.claude_code import SessionRef

    return SessionRef(
        session_id="sid",
        name="a-session",
        cwd=str(project),
        started_at=started or datetime.now(UTC),
    )


def hook_entry(command: str) -> list[dict]:
    return [{"hooks": [{"type": "command", "command": command}]}]


def test_a_hook_path_from_another_machine_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard import doctor

    project = gated_project(
        tmp_path,
        {"PreToolUse": hook_entry("/Users/someone-else/halyard-fleet/bridge/hook.sh")},
    )
    monkeypatch.setattr(doctor, "find_session", lambda name: a_session(project), raising=False)
    monkeypatch.setattr("halyard.agents.claude_code.find_session", lambda name: a_session(project))

    lines, problems = doctor._check_gated_project("navigator", "a-session")

    assert problems >= 1
    assert any("does not exist on this machine" in line for line in lines)


def test_a_project_with_no_settings_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard import doctor

    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr("halyard.agents.claude_code.find_session", lambda name: a_session(project))

    lines, problems = doctor._check_gated_project("navigator", "a-session")

    assert problems == 1
    assert any("nothing is gating this project" in line for line in lines)


def test_a_project_without_a_pretooluse_hook_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard import doctor

    project = gated_project(tmp_path, {"Stop": hook_entry(str(BRIDGE_DIR / "relay.py"))})
    monkeypatch.setattr("halyard.agents.claude_code.find_session", lambda name: a_session(project))

    lines, problems = doctor._check_gated_project("navigator", "a-session")

    assert problems >= 1
    assert any("approvals will never be asked for" in line for line in lines)


def test_a_session_older_than_its_settings_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from halyard import doctor

    project = gated_project(tmp_path, {"PreToolUse": hook_entry(str(BRIDGE_DIR / "hook.sh"))})
    stale = datetime.now(UTC) - timedelta(days=3)
    monkeypatch.setattr(
        "halyard.agents.claude_code.find_session", lambda name: a_session(project, started=stale)
    )

    lines, problems = doctor._check_gated_project("navigator", "a-session")

    # Hooks are snapshotted at startup, so a session older than the settings is
    # running with the previous ones and nothing says so anywhere else.
    assert problems == 0
    assert any("restart it" in line for line in lines)


def test_a_correctly_wired_project_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from halyard import doctor

    project = gated_project(
        tmp_path,
        {
            "PreToolUse": hook_entry("$CLAUDE_PROJECT_DIR/../bridge/hook.sh"),
            "Stop": hook_entry(str(BRIDGE_DIR / "relay.py")),
        },
    )
    # $CLAUDE_PROJECT_DIR is expanded before the path is checked.
    (tmp_path / "bridge").mkdir(exist_ok=True)
    wrapper = tmp_path / "bridge" / "hook.sh"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr("halyard.agents.claude_code.find_session", lambda name: a_session(project))

    lines, problems = doctor._check_gated_project("navigator", "a-session")

    assert problems == 0
    assert any("PreToolUse" in line for line in lines)
