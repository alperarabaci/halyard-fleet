"""What the runner actually asks the CLI to do.

Everything here is about the command line that gets built. That line decides
which model does the work, and it is the one part of sending a message that
cannot be checked by reading a reply: a turn answered by the wrong model still
answers, plausibly, and says nothing about it.
"""

from __future__ import annotations

import asyncio

import pytest

from halyard.agents.claude_code.runner import DEFAULT_MODEL, ClaudeCodeRunner

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


async def test_a_turn_runs_on_sonnet_when_nobody_has_said_otherwise(monkeypatch) -> None:
    """The CLI's own default is haiku, which is not a default for real work.

    Measured: `claude -p` with no --model answered as claude-haiku-4-5. A
    message sent from a phone continues work in a real codebase, so leaving
    that unset would quietly put every one of them on the cheapest model
    available — visible only in the quality of the answer.
    """
    calls = spying(monkeypatch)

    await runner().send("session-1", "carry on")

    assert calls[0][calls[0].index("--model") :][:2] == ["--model", DEFAULT_MODEL]


async def test_the_default_can_be_replaced_from_the_environment(monkeypatch) -> None:
    calls = spying(monkeypatch)

    await runner(default_model="opus").send("session-1", "carry on")

    assert "opus" in calls[0]
    assert DEFAULT_MODEL not in calls[0]


async def test_no_model_is_sent_when_the_default_is_cleared(monkeypatch) -> None:
    """An empty setting means "do not pass --model", not "fall back to ours"."""
    calls = spying(monkeypatch)

    await runner(default_model=None).send("session-1", "carry on")

    assert "--model" not in calls[0]


async def test_a_chosen_model_beats_the_default(monkeypatch) -> None:
    calls = spying(monkeypatch)
    made = runner()

    made.set_model("session-1", "fable")
    await made.send("session-1", "carry on")

    assert "fable" in calls[0]
    assert DEFAULT_MODEL not in calls[0]


async def test_clearing_a_choice_returns_to_the_default_not_to_the_cli(monkeypatch) -> None:
    """Where "clear" lands is the whole question.

    It cannot hand the choice back to the session — nothing in this process can
    reach what the app is set to — so it lands on this control plane's default.
    Landing on the CLI's instead would silently be haiku.
    """
    calls = spying(monkeypatch)
    made = runner()

    made.set_model("session-1", "fable")
    made.set_model("session-1", None)
    await made.send("session-1", "carry on")

    assert DEFAULT_MODEL in calls[0]
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
    assert DEFAULT_MODEL in calls[1]


async def test_preferences_report_what_will_happen_not_what_was_typed() -> None:
    """Reporting only the override would print nothing in the ordinary case.

    Which is exactly when somebody asks — before sending an expensive
    instruction, to check where it is going.
    """
    made = runner()

    assert made.preferences("session-1") == (DEFAULT_MODEL, None)

    made.set_effort("session-1", "xhigh")
    assert made.preferences("session-1") == (DEFAULT_MODEL, "xhigh")
