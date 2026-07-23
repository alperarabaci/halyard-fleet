"""What the runner actually asks the CLI to do.

Everything here is about the command line that gets built. That line decides
which model does the work, and it is the one part of sending a message that
cannot be checked by reading a reply: a turn answered by the wrong model still
answers, plausibly, and says nothing about it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from halyard.agents.claude_code import runner as runner_module
from halyard.agents.claude_code.runner import ClaudeCodeRunner

pytestmark = pytest.mark.asyncio


class FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def spying(monkeypatch) -> list[list[str]]:
    """Capture argument lists instead of starting anything."""
    calls: list[list[str]] = []

    async def fake_exec(*arguments, **_kwargs):
        calls.append(list(arguments))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def runner(**kwargs) -> ClaudeCodeRunner:
    made = ClaudeCodeRunner(**kwargs)
    # Stand in for a CLI that may not be installed wherever this runs.
    made._binary = "/usr/bin/claude"
    return made


async def test_desktop_engine_is_preferred_over_a_different_cli_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop_root = tmp_path / "claude-code"
    older = desktop_root / "2.1.99" / "claude.app" / "Contents" / "MacOS" / "claude"
    current = desktop_root / "2.1.217" / "claude.app" / "Contents" / "MacOS" / "claude"
    older.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    older.touch()
    current.touch()
    monkeypatch.setattr(runner_module, "_DESKTOP_CLAUDE_CODE_DIR", desktop_root)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/standalone/claude")

    assert runner_module.find_claude_binary() == str(current)


async def test_explicit_claude_binary_overrides_desktop_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop_root = tmp_path / "claude-code"
    bundled = desktop_root / "2.1.217" / "claude.app" / "Contents" / "MacOS" / "claude"
    bundled.parent.mkdir(parents=True)
    bundled.touch()
    explicit = tmp_path / "claude"
    explicit.touch()
    monkeypatch.setattr(runner_module, "_DESKTOP_CLAUDE_CODE_DIR", desktop_root)

    assert runner_module.find_claude_binary(str(explicit)) == str(explicit)


async def test_a_turn_inherits_the_resumed_session_model_by_default(monkeypatch) -> None:
    """A live Desktop-owned opus session stayed on opus without --model.

    The haiku measurement was a fresh headless prompt, not a resume. Applying
    it here introduced a model override that the working desktop path did not
    have.
    """
    calls = spying(monkeypatch)

    await runner().send("session-1", "carry on")

    assert "--model" not in calls[0]


async def test_a_model_override_can_be_configured_from_the_environment(monkeypatch) -> None:
    calls = spying(monkeypatch)

    await runner(default_model="opus").send("session-1", "carry on")

    assert "opus" in calls[0]


async def test_explicit_none_still_preserves_session_model_inheritance(monkeypatch) -> None:
    calls = spying(monkeypatch)

    await runner(default_model=None).send("session-1", "carry on")

    assert "--model" not in calls[0]


async def test_a_chosen_model_beats_session_inheritance(monkeypatch) -> None:
    calls = spying(monkeypatch)
    made = runner()

    made.set_model("session-1", "fable")
    await made.send("session-1", "carry on")

    assert "fable" in calls[0]


async def test_clearing_a_choice_restores_session_inheritance(monkeypatch) -> None:
    calls = spying(monkeypatch)
    made = runner()

    made.set_model("session-1", "fable")
    made.set_model("session-1", None)
    await made.send("session-1", "carry on")

    assert "--model" not in calls[0]
    assert "fable" not in calls[0]


async def test_a_choice_belongs_to_one_session_only(monkeypatch) -> None:
    """A navigator and a driver are split precisely so they can differ."""
    calls = spying(monkeypatch)
    made = runner()

    made.set_model("session-nav", "opus")
    await made.send("session-nav", "think about this")
    await made.send("session-drv", "do this")

    assert "opus" in calls[0]
    assert "opus" not in calls[1]
    assert "--model" not in calls[1]


async def test_preferences_report_what_will_happen_not_what_was_typed() -> None:
    """None means the resumed session/runtime owns the choice."""
    made = runner()

    assert made.preferences("session-1") == (None, None)

    made.set_effort("session-1", "xhigh")
    assert made.preferences("session-1") == (None, "xhigh")
