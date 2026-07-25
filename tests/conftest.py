"""Shared test setup.

One fixture, and it exists for a reason this repository has already written a
postmortem about: a test whose outcome depends on the machine running it is not
a test of the code. Seven wiring tests were green in CI through a fallback and
on the developer's Mac by coincidence, and would have failed on the machine
that actually broke.

Configuration is now one file — `halyard.yaml`, found by walking up from the
working directory — so any test that builds `Settings` or reads seats picks up
the developer's real configuration unless something stops it. Their bot token,
their chat ids, their five seats. That makes assertions pass for reasons that
have nothing to do with the assertion, and it would make a stranger's checkout
behave differently from this one.

So every test starts somewhere empty, and a test that wants configuration
writes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _away_from_the_real_configuration(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run each test from an empty directory, with no inherited settings.

    The environment is cleared of Halyard's own variables as well as the file:
    a real variable still outranks the file by design, so one left over in the
    shell that ran `pytest` would quietly configure a test.
    """
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    for name in list(__import__("os").environ):
        if name.startswith(("HALYARD_", "TELEGRAM_", "CLAUDE_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def anywhere(tmp_path: Path) -> Path:
    """A directory to put a `halyard.yaml` in, for tests that want one."""
    return tmp_path
