"""From a configuration file to a card in the right group, in one test.

Every piece of this chain was tested on its own and the chain still broke
twice: seats loaded correctly while the control plane saw none of them, and a
reply reached the channel with a role but no identity to route by. Each part
passed; the joins were where the failures lived.

Both Codex postmortems end at the same place. *An adapter boundary is not
complete when it can resolve an identifier; it is complete when the identifier
reaches the operation together with the runtime that gives it meaning.* This
test walks that whole distance — environment, `create_app`, the real channel —
and asserts a card from each seat arrives where that seat speaks.

Nothing here reaches the network. `TelegramChannel` opens no connection when it
is constructed; polling starts in `start()`, which the app calls only inside its
lifespan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard.api.app import create_app
from halyard.config import Settings
from halyard.core.events import Role

FOUR_SEATS = {
    "HALYARD_CHANNEL": "telegram",
    "TELEGRAM_BOT_TOKEN": "123:not-a-real-token",
    "TELEGRAM_CHAT_ID": "-9999",
    "TELEGRAM_AUTHORIZED_USER_IDS": "4242",
    "HALYARD_SEATS": "nav,drv,xnav,xdrv",
    "HALYARD_SEAT_NAV": "runtime=claude-code session=a-nav chat=-1001 role=navigator",
    "HALYARD_SEAT_DRV": "runtime=claude-code session=a-drv chat=-1002 role=driver",
    "HALYARD_SEAT_XNAV": "runtime=codex session=x-nav chat=-1003 role=navigator",
    "HALYARD_SEAT_XDRV": "runtime=codex session=x-drv chat=-1004 role=driver",
}


@pytest.fixture
def app_with_four_seats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A control plane built the way the real one is built.

    Configured through a real `.env` file rather than environment variables,
    because that is where the configuration actually lives and where it once
    went unread: seats were looked for in `os.environ` while everything else
    came from the file, so four correct seats produced a control plane holding
    none. A test that sets environment variables would have passed throughout.

    Run from an empty directory so the repository's own `.env` cannot leak into
    the result — a test that passes because of the developer's configuration is
    not a test.
    """
    monkeypatch.chdir(tmp_path)
    for stray in (*FOUR_SEATS, "HALYARD_NAVIGATOR_SESSION", "HALYARD_DRIVER_SESSION"):
        monkeypatch.delenv(stray, raising=False)

    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in FOUR_SEATS.items())
        + f"\nHALYARD_DB_PATH={tmp_path / 'halyard.db'}"
        + f"\nHALYARD_AUDIT_LOG={tmp_path / 'audit.jsonl'}\n"
    )

    app = create_app(Settings())
    return app


def test_the_control_plane_sees_every_configured_seat(app_with_four_seats) -> None:
    """Seats were once configured correctly and invisible to the process.

    They were read from `os.environ` while everything else was read from
    `.env`, so four correct seats produced a control plane that reported none —
    and `doctor` saying "no seats configured" was the only sign of it.
    """
    channel = app_with_four_seats.state.channel

    assert [seat.label for seat in channel._seats] == ["nav", "drv", "xnav", "xdrv"]


@pytest.mark.parametrize(
    ("session_name", "agent_id", "chat"),
    [
        ("a-nav", "claude-code", "-1001"),
        ("a-drv", "claude-code", "-1002"),
        ("x-nav", "codex", "-1003"),
        ("x-drv", "codex", "-1004"),
    ],
)
def test_a_card_reaches_the_group_its_seat_speaks_in(
    app_with_four_seats, session_name: str, agent_id: str, chat: str
) -> None:
    """The routing failure, from configuration rather than from a constructor.

    `role=None` on purpose: a session started from a desktop app sets no
    `HALYARD_ROLE`, so this is what a real card carries, and the session name is
    the only thing that can place it. Two seats share the role `driver`.
    """
    channel = app_with_four_seats.state.channel

    destination, _thread = channel._route(None, session_name, agent_id)

    assert destination == chat
    assert destination != "-9999", "fell through to the bot's own chat"


def test_each_seat_is_answered_by_its_own_runtime(app_with_four_seats) -> None:
    """From the postmortem, stated as a rule: a session address is
    `(runtime, session_id)`, never a bare session id.

    A Codex seat that reached the Claude Code runner produced `No conversation
    found with session ID` — the resolver was runtime-aware and the delivery
    that followed it was not.
    """
    channel = app_with_four_seats.state.channel
    seats = {seat.label: seat for seat in channel._seats}

    assert channel._runner_for(seats["drv"]).id == "claude-code"
    assert channel._runner_for(seats["xdrv"]).id == "codex"


def test_an_unknown_session_lands_in_the_default_chat(app_with_four_seats) -> None:
    """The fallback has to stay a fallback.

    Somewhere for an unplaceable card to go is necessary; a seat borrowing
    another seat's group would be worse than the bot talking to itself.
    """
    channel = app_with_four_seats.state.channel

    destination, _thread = channel._route(None, "a-session-nobody-configured", "codex")

    assert destination == "-9999"


def test_health_reports_the_seats_it_is_actually_holding(app_with_four_seats) -> None:
    """Visible from outside, because the failure mode is a control plane that
    looks healthy while routing nothing."""
    seats = {seat.label: seat.runtime for seat in app_with_four_seats.state.channel._seats}

    assert seats == {
        "nav": "claude-code",
        "drv": "claude-code",
        "xnav": "codex",
        "xdrv": "codex",
    }


def test_a_configuration_from_before_seats_still_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody should have to rewrite a working setup to keep it working.

    This is the shape every installation had before a second runtime existed,
    and it has to keep meaning what it meant.
    """
    monkeypatch.chdir(tmp_path)
    for stray in FOUR_SEATS:
        monkeypatch.delenv(stray, raising=False)
    for key, value in {
        "HALYARD_CHANNEL": "telegram",
        "TELEGRAM_BOT_TOKEN": "123:not-a-real-token",
        "TELEGRAM_CHAT_ID": "-9999",
        "TELEGRAM_AUTHORIZED_USER_IDS": "4242",
        "HALYARD_NAVIGATOR_SESSION": "a-nav",
        "TELEGRAM_NAVIGATOR_CHAT_ID": "-1001",
        "HALYARD_DRIVER_SESSION": "a-drv",
        "TELEGRAM_DRIVER_CHAT_ID": "-1002",
        "HALYARD_DB_PATH": str(tmp_path / "halyard.db"),
        "HALYARD_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    }.items():
        monkeypatch.setenv(key, value)

    channel = create_app(Settings()).state.channel

    assert channel._route(Role.NAVIGATOR)[0] == "-1001"
    assert channel._route(Role.DRIVER)[0] == "-1002"
