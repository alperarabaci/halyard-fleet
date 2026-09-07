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

#: One file, as a person would write it. Settings and seats together, which is
#: the point: they used to be two files and neither said which one won.
CONFIGURATION = """\
settings:
  HALYARD_CHANNEL: telegram
  TELEGRAM_BOT_TOKEN: "123:not-a-real-token"
  TELEGRAM_CHAT_ID: "-9999"
  TELEGRAM_AUTHORIZED_USER_IDS: "4242"
  HALYARD_DB_PATH: {db}
  HALYARD_AUDIT_LOG: {audit}

projects:
  a-project:
    seats:
      nav: {{runtime: claude-code, session: a-nav, chat: "-1001", role: navigator}}
      drv: {{runtime: claude-code, session: a-drv, chat: "-1002", role: driver}}
      xnav: {{runtime: codex, session: x-nav, chat: "-1003", role: navigator}}
      xdrv: {{runtime: codex, session: x-drv, chat: "-1004", role: driver}}
"""


@pytest.fixture
def app_with_four_seats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A control plane built the way the real one is built.

    Configured through a real `halyard.yaml` rather than environment variables,
    because that is where the configuration actually lives and where it once
    went unread: seats were looked for in `os.environ` while everything else
    came from a file, so four correct seats produced a control plane holding
    none. A test that sets environment variables would have passed throughout.

    The settings and the seats are in one document here because they are in one
    document now. They were two files for a while, with no rule written down
    for which of them won.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "halyard.yaml").write_text(
        CONFIGURATION.format(db=tmp_path / "halyard.db", audit=tmp_path / "audit.jsonl")
    )

    return create_app(Settings())


def test_the_control_plane_sees_every_configured_seat(app_with_four_seats) -> None:
    """Seats were once configured correctly and invisible to the process.

    They were read from `os.environ` while everything else was read from a
    file, so four correct seats produced a control plane that reported none —
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

    Set as real environment variables, which is still a supported way in — the
    file replaced `.env`, not the environment. A container passing a token in
    should not have to write it to disk first.
    """
    monkeypatch.chdir(tmp_path)
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


#: The same chain for a runtime whose sessions cannot be named. Two projects,
#: because the point is that the codebase is what tells the seats apart.
WITHOUT_SESSION_NAMES = """\
settings:
  HALYARD_CHANNEL: telegram
  TELEGRAM_BOT_TOKEN: "123:not-a-real-token"
  TELEGRAM_CHAT_ID: "-9999"
  TELEGRAM_AUTHORIZED_USER_IDS: "4242"
  HALYARD_DB_PATH: {db}
  HALYARD_AUDIT_LOG: {audit}

projects:
  a-project:
    seats:
      onav: {{runtime: opencode, chat: "-2001", role: navigator}}
      nav: {{runtime: claude-code, session: a-nav, chat: "-1001", role: navigator}}
  another-project:
    seats:
      onav2: {{runtime: opencode, chat: "-2002", role: navigator}}
"""


@pytest.fixture
def app_without_session_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "halyard.yaml").write_text(
        WITHOUT_SESSION_NAMES.format(db=tmp_path / "halyard.db", audit=tmp_path / "audit.jsonl")
    )
    return create_app(Settings())


def test_a_seat_with_no_session_name_is_reached_by_its_project(
    app_without_session_names,
) -> None:
    """opencode writes its own session titles from the conversation and hands
    out ids nobody types, so a seat there is written without a `session:`. The
    codebase is what places the card — and it has to place it in the right one
    of two, which routing by role could not do."""
    channel = app_without_session_names.state.channel

    assert channel._route(None, None, "opencode", "ses_abc", "a-project")[0] == "-2001"
    assert channel._route(None, None, "opencode", "ses_xyz", "another-project")[0] == "-2002"


def test_the_project_does_not_outrank_a_session_name(app_without_session_names) -> None:
    """A seat that can be addressed by name still is. The project is what is
    tried when nothing else identifies the seat, not instead of it."""
    channel = app_without_session_names.state.channel

    assert channel._route(None, "a-nav", "claude-code", None, "a-project")[0] == "-1001"


def test_an_unconfigured_project_falls_back_rather_than_guessing(
    app_without_session_names,
) -> None:
    """It reaches the bot's own chat, which is visible and wrong-looking. A
    guess would reach somebody's group and look right."""
    channel = app_without_session_names.state.channel

    assert (
        channel._route(None, None, "opencode", "ses_abc", "a-project-nobody-configured")[0]
        == "-9999"
    )


async def test_a_reply_from_a_seat_with_no_session_name_reaches_its_group(
    app_without_session_names,
) -> None:
    """A reply carries the id of the session it came from, and nothing that was
    ever written in a configuration. So the name matches no seat, the message
    falls through to whatever the role happens to point at, and it arrives in
    somebody else's group — which is what happened the first time an opencode
    reply came back.

    The codebase is what places it, the same way the card is placed.
    """
    channel = app_without_session_names.state.channel
    sent: list[tuple[str, str]] = []

    async def record(chat_id, text, **rest):
        sent.append((chat_id, text))
        return {"message_id": 1}

    channel._api.send_message = record

    await channel.send_message(
        "ses_nobody_configured", "done", None, agent_id="opencode", project="a-project"
    )

    assert sent and sent[0][0] == "-2001", "the reply went somewhere else"


async def test_a_reply_with_no_project_still_goes_somewhere_visible(
    app_without_session_names,
) -> None:
    """The bot's own chat, which is visibly wrong. A guess would be invisibly
    wrong, in a colleague's group."""
    channel = app_without_session_names.state.channel
    sent: list[tuple[str, str]] = []

    async def record(chat_id, text, **rest):
        sent.append((chat_id, text))
        return {"message_id": 1}

    channel._api.send_message = record

    await channel.send_message("ses_x", "done", None, agent_id="opencode")

    assert sent and sent[0][0] == "-9999"
