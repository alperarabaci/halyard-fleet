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


def test_the_path_finds_a_uv_installed_where_uv_installs_itself() -> None:
    """The fixed list did not include `~/.local/bin`, which is where uv's own
    installer puts it — and a project's `make` target calling `uv sync` failed
    with `make: uv: No such file or directory` on a machine where uv was
    perfectly installed. Halyard itself started fine there, because the service
    is launched by absolute path and never consults this. Only what it *runs*
    was affected, which is what made it hard to see.
    """
    where = service._path_for("/Users/someone/.local/bin/uv", "/usr/bin/git").split(":")

    assert "/Users/someone/.local/bin" in where
    assert where[0] == "/Users/someone/.local/bin", "the uv this service uses comes first"


def test_the_path_keeps_the_places_a_mac_puts_things() -> None:
    """Widening it must not narrow it: a Makefile reaching for something in
    /opt/homebrew/bin has to go on working."""
    where = service._path_for("/Users/someone/.local/bin/uv", "/usr/bin/git").split(":")

    for usual in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        assert usual in where


def test_the_path_does_not_follow_a_symlink_into_a_versioned_directory() -> None:
    """`/opt/homebrew/bin/uv` points into a Cellar directory named for the
    version. Resolving it would write a path that the next `brew upgrade`
    deletes, and the service would break on a day nobody touched it.
    """
    where = service._path_for("/opt/homebrew/bin/uv", "/usr/bin/git")

    assert "/opt/homebrew/bin" in where.split(":")
    assert "Cellar" not in where


def test_no_directory_is_listed_twice() -> None:
    """Both tools in one directory is the ordinary case."""
    where = service._path_for("/usr/local/bin/uv", "/usr/local/bin/git").split(":")

    assert len(where) == len(set(where))


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


def test_restarting_a_running_service_uses_kickstart(macos, monkeypatch, tmp_path) -> None:
    """`bootout` returns before launchd has finished, so tearing down and
    bootstrapping again fails on a service that is already up:

        Bootstrap failed: 5: Input/output error

    which reads like a permissions problem and is not one. `kickstart -k` is
    atomic and has no gap to race in.
    """
    calls: list[tuple[str, ...]] = []
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    # A fake that answers everything successfully means `print` succeeds, which
    # is what "already loaded" looks like.
    monkeypatch.setattr(service, "_launchctl", lambda *a: (calls.append(a), _ok(a))[1])

    assert service.restart() == 0

    names = [args[0] for args in calls]
    assert "kickstart" in names
    assert "bootstrap" not in names


def test_restarting_a_stopped_service_enables_then_bootstraps(macos, monkeypatch, tmp_path) -> None:
    """Nothing loaded, so there is nothing to kickstart — and enabling first in
    case a `launchctl unload -w` left the record that hides the service."""
    calls: list[tuple[str, ...]] = []
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)

    def answer(*args: str):
        calls.append(args)
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 1, "", "not loaded")
        return _ok(args)

    monkeypatch.setattr(service, "_launchctl", answer)

    assert service.restart() == 0

    names = [args[0] for args in calls]
    assert "kickstart" not in names
    assert names.index("enable") < names.index("bootstrap")


def test_a_bootstrap_that_loses_the_race_is_retried(macos, monkeypatch, tmp_path) -> None:
    """The teardown is asynchronous and there is no "wait until gone" to call,
    so the answer is to ask again rather than to fail in front of somebody."""
    monkeypatch.setattr(service, "BOOTSTRAP_PAUSE_SECONDS", 0)
    attempts = []

    def answer(*args: str):
        if args[0] == "bootstrap":
            attempts.append(args)
            if len(attempts) < 3:
                return subprocess.CompletedProcess(
                    args, 5, "", "Bootstrap failed: 5: Input/output error"
                )
        return _ok(args)

    monkeypatch.setattr(service, "_launchctl", answer)

    assert service._bootstrap(tmp_path / "agent.plist").returncode == 0
    assert len(attempts) == 3


def test_a_bootstrap_that_never_succeeds_gives_up_with_what_launchd_said(
    macos, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(service, "BOOTSTRAP_PAUSE_SECONDS", 0)
    monkeypatch.setattr(
        service,
        "_launchctl",
        lambda *a: subprocess.CompletedProcess(a, 5, "", "Bootstrap failed: 5: Input/output error"),
    )

    done = service._bootstrap(tmp_path / "agent.plist")

    assert done.returncode == 5
    assert "Input/output error" in done.stderr


def test_restart_says_so_when_nothing_is_installed(macos, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "missing.plist")

    assert service.restart() == 1
    assert "not installed" in capsys.readouterr().out


def test_stop_leaves_no_disabled_record(macos, monkeypatch, tmp_path, capsys) -> None:
    """`bootout`, never `unload -w` — the latter is what left the service
    unfindable for an afternoon, and this is the command that replaces it."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(service, "_launchctl", lambda *a: (calls.append(a), _ok(a))[1])

    assert service.stop() == 0

    assert [args[0] for args in calls] == ["bootout"]
    # And it says what stopping costs, because the gate is down meanwhile.
    assert "deny every command" in capsys.readouterr().out


def test_stop_keeps_the_agent_installed(macos, monkeypatch, tmp_path) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _ok(a))

    service.stop()

    assert plist.exists()


# --- what the service can find, as opposed to what this shell can -------------


def _agent_with(path_value: str, where: Path) -> Path:
    plist = where / "com.halyard.fleet.plist"
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"PATH": path_value}}))
    return plist


def _tool(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    made = directory / name
    made.write_text("#!/bin/sh\n")
    made.chmod(0o755)


def test_doctor_says_nothing_when_there_is_no_service(tmp_path: Path) -> None:
    """`halyard serve` by hand inherits a shell's PATH, which is somebody
    else's business."""
    from halyard import doctor

    lines, problems = doctor.check_service_path(tmp_path / "not-installed.plist")

    assert lines == []
    assert problems == 0


def test_doctor_passes_when_the_agent_can_find_its_tools(tmp_path: Path) -> None:
    from halyard import doctor

    bin_dir = tmp_path / "bin"
    for tool in doctor.SERVICE_NEEDS:
        _tool(bin_dir, tool)

    lines, problems = doctor.check_service_path(_agent_with(str(bin_dir), tmp_path))

    assert problems == 0
    assert any("can find" in line for line in lines)


def test_doctor_catches_the_tool_the_agent_cannot_find(tmp_path: Path) -> None:
    """The failure this exists for. `make bootstrap-up` came back as
    `make: uv: No such file or directory` on a machine where uv was installed —
    to `~/.local/bin`, which the agent's PATH did not include. Halyard itself
    ran perfectly throughout, because it is started by absolute path and never
    reads this.
    """
    from halyard import doctor

    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "git")  # git yes, uv no — which is exactly how it happened

    lines, problems = doctor.check_service_path(_agent_with(str(bin_dir), tmp_path))

    assert problems == 1
    assert any("cannot find uv" in line for line in lines)
    assert any("halyard service install" in line for line in lines), "say what fixes it"


def test_doctor_reports_where_this_shell_finds_it_instead(tmp_path: Path) -> None:
    """The difference between the two PATHs is the whole diagnosis, and a
    person reading `doctor` in a terminal has the other one in front of them.
    """
    from halyard import doctor

    lines, _ = doctor.check_service_path(_agent_with(str(tmp_path / "empty"), tmp_path))

    assert any("this shell finds" in line for line in lines)


def test_doctor_survives_a_plist_it_cannot_parse(tmp_path: Path) -> None:
    """Nothing in the gate depends on this check."""
    from halyard import doctor

    broken = tmp_path / "com.halyard.fleet.plist"
    broken.write_bytes(b"not a plist at all")

    lines, problems = doctor.check_service_path(broken)

    assert problems == 0
    assert lines and "could not read" in lines[0]
