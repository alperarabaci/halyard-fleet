"""Keeping the machine awake while the gate is the gate.

The failure this prevents took a while to recognise: approvals stopped
arriving on a phone and started appearing in a desktop app instead, hours
after somebody closed a screen-sharing window. `pmset -g assertions` on that
machine showed the only two wake assertions both belonged to that window.
"""

from __future__ import annotations

import subprocess

from halyard import awake


def test_nothing_is_held_when_it_is_turned_off(monkeypatch) -> None:
    """Off means no child process at all, not a child that does nothing."""
    started: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_: started.append(argv))

    with awake.held(enabled=False) as holding:
        assert holding is False

    assert started == []


def test_the_assertion_is_tied_to_this_process(monkeypatch) -> None:
    """`-w <pid>`: a control plane that is killed outright must not leave a
    machine awake for something that is no longer running."""
    import os

    started: list[list[str]] = []

    class Fake:
        def terminate(self) -> None: ...
        def wait(self, timeout=None) -> None: ...

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/caffeinate")
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_: started.append(argv) or Fake())

    with awake.held() as holding:
        assert holding is True

    assert started[0][:3] == ["caffeinate", "-i", "-w"]
    assert started[0][3] == str(os.getpid())


def test_it_is_released_when_serving_stops(monkeypatch) -> None:
    """Held for the life of the block and no longer. The machine belongs to
    whoever is using it once the gate is not the gate."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/caffeinate")
    ended: list[str] = []

    class Fake:
        def terminate(self) -> None:
            ended.append("terminated")

        def wait(self, timeout=None) -> None:
            ended.append("waited")

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: Fake())

    with awake.held():
        pass

    assert ended == ["terminated", "waited"]


def test_a_machine_that_cannot_be_kept_awake_still_serves(monkeypatch) -> None:
    """A gate that would not start because it could not prevent a nap would be
    worse than the nap."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/caffeinate")

    def refuse(*_a, **_k):
        raise OSError("no")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    with awake.held() as holding:
        assert holding is False


def test_only_macos_is_attempted(monkeypatch) -> None:
    """`caffeinate` is a macOS command. Elsewhere this says so by holding
    nothing, rather than by failing."""
    monkeypatch.setattr("sys.platform", "linux")

    with awake.held() as holding:
        assert holding is False
