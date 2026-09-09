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
    # A real executable standing in for a CLI that may not be installed
    # wherever this runs. It has to exist: the runner resolves its path when it
    # needs it rather than remembering one from startup, so that a CLI
    # installed later is found without a restart — and an upgrade that moves
    # the binary under a new version number does not strand a running control
    # plane. A configured path that is not there is correctly no path at all.
    made._configured = "/bin/sh"
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


# --- the credential these turns run on ---------------------------------------
#
# The login `/login` creates is refreshed while somebody is at the keyboard and
# eventually cannot be. Measured twice, four days apart: deliveries stopped with
# "OAuth session expired and could not be refreshed" until somebody signed in at
# the desk — which is the one thing a control plane for working away from the
# desk cannot ask for.


def spying_on_the_environment(monkeypatch) -> list[dict]:
    """Capture the environment each delivery would run in."""
    seen: list[dict] = []

    async def fake_exec(*_arguments, **kwargs):
        seen.append(kwargs.get("env") or {})
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


async def test_a_configured_token_reaches_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude setup-token` mints one that lasts about a year and uses the
    subscription. This is how it gets to the process that needs it."""
    seen = spying_on_the_environment(monkeypatch)

    await runner(oauth_token="sk-ant-oat-example").send("session-1", "carry on")

    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-example"


async def test_a_configured_token_replaces_an_inherited_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set, not defaulted. The point is that these turns stop depending on
    whatever the surrounding environment happens to hold, so a stale inherited
    value must not win over the one that was configured."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-from-the-shell")
    seen = spying_on_the_environment(monkeypatch)

    await runner(oauth_token="sk-ant-oat-configured").send("session-1", "carry on")

    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-configured"


async def test_without_a_token_the_environment_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installation that has not configured one keeps working exactly as it
    did, on whatever credential the CLI already had."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    seen = spying_on_the_environment(monkeypatch)

    await runner().send("session-1", "carry on")

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen[0]


async def test_a_blank_token_is_not_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty setting is somebody who has not filled it in, not somebody
    asking for an empty credential — which would authenticate as nobody."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    seen = spying_on_the_environment(monkeypatch)

    await runner(oauth_token="   ").send("session-1", "carry on")

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen[0]


async def test_the_token_never_reaches_the_argument_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arguments are visible to anyone who can run `ps`. A credential travels
    in the environment of one subprocess and nowhere else."""
    calls = spying(monkeypatch)

    await runner(oauth_token="sk-ant-oat-secret").send("session-1", "carry on")

    assert not any("sk-ant-oat-secret" in argument for argument in calls[0])


async def test_an_api_key_that_outranks_the_token_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """ANTHROPIC_API_KEY wins over the token *and* bills the API rather than the
    subscription, so an inherited one quietly changes who pays."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-inherited")
    spying_on_the_environment(monkeypatch)

    with caplog.at_level("WARNING"):
        await runner(oauth_token="sk-ant-oat-example").send("session-1", "carry on")

    assert "outranks" in caplog.text
    # The warning explains the consequence, and quotes neither credential.
    assert "sk-ant-api-inherited" not in caplog.text
    assert "sk-ant-oat-example" not in caplog.text


# --- a directory macOS will not open ------------------------------------------
#
# `~/Library/Application Support/Claude/claude-code` is where the app keeps the
# engine this runner prefers, and macOS counts it as another application's data.
# Measured on a Mac mini: the first read prompted for
# `kTCCServiceSystemPolicyAppData`, naming `uv` — the service is started with
# `uv run`, so `uv` is the responsible process — and `uv` has no stable signing
# identity, so the grant is pinned to that binary and a `uv` upgrade asks again.
# Nobody is sitting at a headless machine to answer it.
#
# What makes that worth a check rather than a shrug is how it fails. `glob`
# swallows the permission error and finds nothing, which is indistinguishable
# from Claude Desktop not being installed, and the runner then quietly uses
# whatever `claude` is on PATH.


async def test_a_refused_directory_is_not_the_same_as_a_missing_one(
    monkeypatch, tmp_path: Path
) -> None:
    """The distinction `glob` cannot make."""
    monkeypatch.setattr(runner_module, "_DESKTOP_CLAUDE_CODE_DIR", tmp_path / "nowhere")
    assert runner_module.desktop_engine_readable() is None

    monkeypatch.setattr(runner_module, "_DESKTOP_CLAUDE_CODE_DIR", tmp_path)
    assert runner_module.desktop_engine_readable() is True

    def refused(_path):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(runner_module.os, "listdir", refused)
    assert runner_module.desktop_engine_readable() is False


async def test_a_refusal_is_reported_rather_than_absorbed(monkeypatch) -> None:
    """Because the cost of it is silent: the preference for the app's own
    engine simply stops applying, and deliveries go on working well enough to
    look fine."""
    from halyard.agents import claude_code

    monkeypatch.setattr(runner_module, "find_claude_binary", lambda *_a, **_k: "/bin/sh")
    monkeypatch.setattr(runner_module, "desktop_engine_readable", lambda: False)
    monkeypatch.setattr(runner_module, "signed_in", lambda *_a, **_k: True)

    said = claude_code.RUNTIME.check_available(claude_oauth_token="t")

    assert any(level == "warn" and "refusing" in text for level, text in said)
    assert any("App Data" in text for _, text in said)


async def test_a_configured_binary_is_not_warned_about(monkeypatch) -> None:
    """Nothing looks in that directory when a path was given, so a refusal
    there costs nothing and saying so would be noise on every check."""
    from halyard.agents import claude_code

    monkeypatch.setattr(runner_module, "find_claude_binary", lambda *_a, **_k: "/bin/sh")
    monkeypatch.setattr(runner_module, "desktop_engine_readable", lambda: False)
    monkeypatch.setattr(runner_module, "signed_in", lambda *_a, **_k: True)

    said = claude_code.RUNTIME.check_available(claude_binary="/bin/sh", claude_oauth_token="t")

    assert not any("refusing" in text for _, text in said)


async def test_a_refusal_with_nothing_to_fall_back_to_is_not_called_missing(monkeypatch) -> None:
    """The Mac mini's shape. Its engine lives only inside the app bundle, so a
    refusal leaves `find_claude_binary` with nothing at all — and the old answer
    for that, "the claude CLI is not on this machine", sends somebody to install
    a CLI that is already installed, on a machine they are away from."""
    from halyard.agents import claude_code

    monkeypatch.setattr(runner_module, "find_claude_binary", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "desktop_engine_readable", lambda: False)

    said = claude_code.RUNTIME.check_available()

    assert any(level == "fail" and "refusing" in text for level, text in said)
    assert not any("not on this machine" in text for _, text in said)
    assert any("App Data" in text for _, text in said)


async def test_a_machine_with_no_claude_at_all_still_says_so(monkeypatch) -> None:
    """The refusal wording must not swallow the plain case."""
    from halyard.agents import claude_code

    monkeypatch.setattr(runner_module, "find_claude_binary", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "desktop_engine_readable", lambda: None)

    said = claude_code.RUNTIME.check_available()

    assert any("not on this machine" in text for _, text in said)
