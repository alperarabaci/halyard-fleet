"""Tests for the launchd service command.

Nothing here calls `launchctl` or `git` for real — the interesting behaviour is
what gets written and what gets skipped, so those are faked. The one rule this
holds to everywhere: a failed update never stops the gate from coming up.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from halyard import service


@pytest.fixture
def macos(monkeypatch) -> None:
    monkeypatch.setattr(service, "_is_macos", lambda: True)


def test_the_plist_updates_then_serves(tmp_path: Path) -> None:
    document = plistlib.loads(
        service.render_plist(tmp_path, "/usr/bin/git", "/opt/homebrew/bin/uv", tmp_path / "l.log")
    )

    assert document["Label"] == service.LABEL
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] is True
    command = document["ProgramArguments"][-1]
    # Order is the whole point: pull, then sync, then serve.
    assert command.index("pull --ff-only") < command.index("uv sync")
    assert command.index("uv sync") < command.index("run halyard serve")


def test_a_failed_update_still_serves(tmp_path: Path) -> None:
    """`;` between the steps, not `&&`: a pull that cannot fast-forward or a sync
    that fails must not stop the gate coming up on the code already here."""
    command = service._serve_command(tmp_path, "/usr/bin/git", "/usr/bin/uv")

    assert " ; " in command
    assert "&&" in command.split(" ; ")[0]  # only the `cd` is guarded with &&
    assert command.endswith("run halyard serve")


def test_the_agent_carries_a_path_that_finds_uv(tmp_path: Path) -> None:
    """launchd's own PATH is /usr/bin:/bin, and uv is usually neither."""
    document = plistlib.loads(
        service.render_plist(tmp_path, "/usr/bin/git", "/opt/homebrew/bin/uv", tmp_path / "l.log")
    )

    assert "/opt/homebrew/bin" in document["EnvironmentVariables"]["PATH"]


def test_off_macos_it_refuses_rather_than_pretends(monkeypatch, capsys) -> None:
    monkeypatch.setattr(service, "_is_macos", lambda: False)

    assert service.install() == 1
    assert "macOS" in capsys.readouterr().out


def test_install_needs_git_and_uv(macos, monkeypatch, capsys) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    assert service.install() == 1
    assert "not on PATH" in capsys.readouterr().out


def test_install_refuses_outside_a_checkout(macos, monkeypatch, capsys) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "_repo_root", lambda start: None)

    assert service.install() == 1
    assert "checkout" in capsys.readouterr().out


def test_install_writes_and_loads_the_agent(macos, monkeypatch, tmp_path, capsys) -> None:
    calls: list[tuple[str, ...]] = []
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "_repo_root", lambda start: tmp_path / "repo")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "log_path", lambda: tmp_path / "logs" / "s.log")
    monkeypatch.setattr(service, "_tracking", lambda repo: ("origin", "main"))

    def fake_launchctl(*args: str):
        calls.append(args)
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_launchctl", fake_launchctl)

    assert service.install() == 0
    # A reload is a reload: the old one is taken down before the new one starts.
    target = f"{service.domain()}/{service.LABEL}"
    assert ("bootout", target) in calls
    assert ("bootstrap", service.domain(), str(plist)) in calls
    assert plistlib.loads(plist.read_bytes())["Label"] == service.LABEL
    # The warning names the remote it will run code from.
    assert "origin/main" in capsys.readouterr().out


def test_install_says_so_when_there_is_nothing_to_pull(macos, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "_repo_root", lambda start: tmp_path / "repo")
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(service, "log_path", lambda: tmp_path / "s.log")
    monkeypatch.setattr(service, "_tracking", lambda repo: None)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _ok(a))

    assert service.install() == 0
    assert "tracks no upstream" in capsys.readouterr().out


def test_install_enables_before_it_loads(macos, monkeypatch, tmp_path) -> None:
    """`launchctl unload -w` writes a persistent disabled record, and the plist
    then sits there looking installed while launchd will not admit the service
    exists. Reinstalling has to clear that, and only `enable` does."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "_repo_root", lambda start: tmp_path / "repo")
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(service, "log_path", lambda: tmp_path / "s.log")
    monkeypatch.setattr(service, "_tracking", lambda repo: None)

    def record(*args: str):
        calls.append(args)
        return _ok(args)

    monkeypatch.setattr(service, "_launchctl", record)
    service.install()

    names = [args[0] for args in calls]
    assert "enable" in names
    assert names.index("enable") < names.index("bootstrap")


def test_status_reports_a_disabled_agent_rather_than_a_loaded_one(
    macos, monkeypatch, tmp_path, capsys
) -> None:
    """The state that cost an afternoon: the plist is there, `install` reported
    success, and launchd says the service does not exist."""
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)

    def answer(*args: str):
        if args[0] == "print-disabled":
            return subprocess.CompletedProcess(args, 0, f'\t\t"{service.LABEL}" => disabled\n', "")
        return _ok(args)

    monkeypatch.setattr(service, "_launchctl", answer)

    assert service.status() == 1
    assert "DISABLED" in capsys.readouterr().out


def test_uninstall_removes_the_agent(macos, monkeypatch, tmp_path, capsys) -> None:
    calls: list[tuple[str, ...]] = []
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "_launchctl", lambda *a: (calls.append(a), _ok(a))[1])

    assert service.uninstall() == 0
    assert not plist.exists()
    # `bootout`, never `unload -w`: the latter leaves the service disabled and
    # the next install then looks like it worked while launchd refuses it.
    assert calls[0][0] == "bootout"


def test_uninstall_is_safe_when_nothing_is_installed(macos, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "missing.plist")

    assert service.uninstall() == 0


def test_status_reports_loaded(macos, monkeypatch, tmp_path) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "log_path", lambda: tmp_path / "s.log")
    monkeypatch.setattr(service, "_launchctl", lambda *a: _ok(a))

    assert service.status() == 0


def test_status_reports_not_installed(macos, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "missing.plist")

    assert service.status() == 1


def test_an_unknown_service_command_is_refused(capsys) -> None:
    assert service.run("frobnicate") == 2
    assert "unknown service command" in capsys.readouterr().out


def _ok(args):
    import subprocess

    return subprocess.CompletedProcess(args, 0, "", "")
