"""`halyard init` — the wizard that writes a `.env`.

The interactive parts take injected `ask`/`secret`/`say` callables, so the whole
flow runs here without a terminal. What matters most is what ends up in the
file: a `.env` this wizard writes has to load back into the seats the person
described, and it must not lose anything it did not ask about.
"""

from __future__ import annotations

from pathlib import Path

from halyard import setup_cli
from halyard.core.events import Role
from halyard.core.seats import Seat, from_environment


def test_the_written_env_loads_back_into_the_same_seats() -> None:
    """The one property that makes the wizard worth having: what it writes is
    what the control plane then reads."""
    text = setup_cli.assemble_env(
        token="123:secret",
        default_chat="-999",
        authorized_ids="4242",
        seats=[
            Seat("nav", "claude-code", "alpha-nav", "-1001", Role.NAVIGATOR),
            Seat("xdrv", "codex", "alpha-xdrv", "-1004", Role.DRIVER),
        ],
        project_name="alpha-engine",
        carried_over={},
    )

    env = _as_env(text)
    loaded = from_environment(env)

    assert [(s.label, s.runtime, s.session, s.chat, s.role) for s in loaded] == [
        ("nav", "claude-code", "alpha-nav", "-1001", Role.NAVIGATOR),
        ("xdrv", "codex", "alpha-xdrv", "-1004", Role.DRIVER),
    ]


def test_a_label_with_a_dash_becomes_a_valid_env_key() -> None:
    """`codex-drv` has to become HALYARD_SEAT_CODEX_DRV, or its line is unread."""
    text = setup_cli.assemble_env(
        token="t",
        default_chat="-1",
        authorized_ids="1",
        seats=[Seat("codex-drv", "codex", "s", "-2")],
        project_name=None,
        carried_over={},
    )

    assert "HALYARD_SEAT_CODEX_DRV=" in text
    assert from_environment(_as_env(text))[0].label == "codex-drv"


def test_unmanaged_keys_are_carried_over() -> None:
    """Re-running to add a seat must not drop the log config from last time."""
    text = setup_cli.assemble_env(
        token="t",
        default_chat="-1",
        authorized_ids="1",
        seats=[],
        project_name=None,
        carried_over={"HALYARD_LOG_LEVEL": "DEBUG", "HALYARD_CLAUDE_DEFAULT_MODEL": "sonnet"},
    )

    assert "HALYARD_LOG_LEVEL=DEBUG" in text
    assert "HALYARD_CLAUDE_DEFAULT_MODEL=sonnet" in text


def test_a_stale_managed_key_is_not_carried_over_twice() -> None:
    """The old token is a default, not a line to copy verbatim beside the new
    one — two TELEGRAM_BOT_TOKEN lines is an ambiguous file."""
    text = setup_cli.assemble_env(
        token="new-token",
        default_chat="-1",
        authorized_ids="1",
        seats=[],
        project_name=None,
        carried_over={"TELEGRAM_BOT_TOKEN": "old-token", "TELEGRAM_CHAT_ID": "-old"},
    )

    assert text.count("TELEGRAM_BOT_TOKEN=") == 1
    assert "old-token" not in text


def test_the_token_is_read_through_secret_never_through_ask(tmp_path: Path) -> None:
    """The one credential must not travel a path that echoes or is recorded.

    `ask` is where a value could be shown or defaulted from a visible place;
    the token has to come from `secret` and nowhere else.
    """
    asked: list[str] = []

    def ask(prompt: str, default: str = "") -> str:
        asked.append(prompt)
        return {"How many Claude Code seats?": "0", "How many Codex seats?": "0"}.get(
            prompt.strip(), default or ""
        )

    def secret(_prompt: str) -> str:
        return "123:the-secret"

    def say(_message: str) -> None:
        pass

    code = setup_cli.run(
        env_path=tmp_path / ".env",
        ask=ask,
        secret=secret,
        say=say,
        now="stamp",
    )

    assert code == 0
    assert not any("token" in prompt.lower() for prompt in asked)
    assert "TELEGRAM_BOT_TOKEN=123:the-secret" in (tmp_path / ".env").read_text()


def test_an_existing_env_is_backed_up_before_being_written(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=old\nHALYARD_LOG_LEVEL=INFO\n")

    setup_cli.run(
        env_path=env,
        ask=lambda prompt, default="": "0" if "How many" in prompt else default,
        secret=lambda _prompt: "new",
        say=lambda _message: None,
        now="20260724-000000",
    )

    backup = env.with_name(".env.20260724-000000.bak")
    assert backup.exists()
    assert "TELEGRAM_BOT_TOKEN=old" in backup.read_text()
    # And the carried-over unmanaged key survived into the new file.
    assert "HALYARD_LOG_LEVEL=INFO" in env.read_text()


def test_no_token_and_no_previous_one_writes_nothing(tmp_path: Path) -> None:
    """A file with no bot token cannot run the gate, so it is not written at
    all rather than written broken."""
    env = tmp_path / ".env"

    code = setup_cli.run(
        env_path=env,
        ask=lambda prompt, default="": default,
        secret=lambda _prompt: "",
        say=lambda _message: None,
    )

    assert code == 1
    assert not env.exists()


def _as_env(text: str) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    }
