"""Seats: the things a message can be sent to.

A seat is a label, a runtime, a session name, and somewhere its traffic goes.
Four of those can be live at once — a Claude navigator, a Claude driver, a Codex
navigator, a Codex driver — and which one you use is decided when you send the
message, not when the process started.

That last part is the whole point, and the earlier design got it wrong. Runtime
used to be a property of a *role*, fixed in the environment: the driver seat was
Claude Code or Codex and changing it meant editing a file, restarting the
control plane, and probably restarting the desktop apps too. Which is a thing
you would need to do exactly when you least want to — a quota running out
mid-afternoon, away from the machine. A control plane you have to go home to
reconfigure is not a control plane.

So every seat you have configured is available all the time, and moving work
between them is a message rather than a deployment.

**Two ways to reach a seat, deliberately.** Each seat may own a chat or a forum
topic, which is how a navigator and a driver stay readable side by side. And any
seat can be named explicitly from anywhere, which is what makes it possible to
take what one seat just wrote and hand it to another.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from halyard.core.events import Role

#: Runtimes a seat can be. Kept here rather than imported from the agent
#: packages so that reading a configuration never imports a CLI wrapper.
KNOWN_RUNTIMES = ("claude-code", "codex")


@dataclass(frozen=True)
class Seat:
    """One addressable place to send a message."""

    #: What you type to reach it. Short, because it is typed on a phone.
    label: str
    runtime: str
    #: The name the runtime knows the session by.
    session: str | None = None
    #: Chat id, optionally with a forum topic after a colon. Seats without one
    #: are reachable by name but have nowhere of their own to speak.
    chat: str | None = None
    #: What the seat is for. Only used to colour a card and to match a hook
    #: payload that declares a role; two seats may share one.
    role: Role | None = None

    def __post_init__(self) -> None:
        if self.runtime not in KNOWN_RUNTIMES:
            raise ValueError(
                f"Seat {self.label!r} has runtime {self.runtime!r}. "
                f"Use one of: {', '.join(KNOWN_RUNTIMES)}."
            )


def _parse_seat(label: str, spec: str) -> Seat:
    """Read `runtime=codex session=thread-name chat=-100123:7`.

    Key/value rather than positional, because a positional list is unreadable
    at exactly the moment it matters — six months later, on a phone, working
    out why a message went somewhere unexpected.
    """
    fields: dict[str, str] = {}
    for part in spec.split():
        key, _, value = part.partition("=")
        if not value:
            raise ValueError(
                f"Seat {label!r}: {part!r} is not `key=value`. "
                "Expected something like `runtime=codex session=my-thread chat=-100123`."
            )
        fields[key.strip().lower()] = value.strip()

    unknown = set(fields) - {"runtime", "session", "chat", "role"}
    if unknown:
        # Silently ignoring a typo would leave a seat missing the setting you
        # thought you gave it, with nothing anywhere saying so.
        raise ValueError(f"Seat {label!r}: unknown field(s) {', '.join(sorted(unknown))}")

    role = fields.get("role")
    return Seat(
        label=label,
        runtime=fields.get("runtime", "claude-code").lower(),
        session=fields.get("session"),
        chat=fields.get("chat"),
        role=Role(role.lower()) if role else None,
    )


def _dotenv(path: Path) -> dict[str, str]:
    """Read a `.env` the way the rest of the configuration is read.

    Seats cannot come through pydantic-settings: their keys are not known
    ahead of time, and a settings class can only declare fields it can name.
    So they are read from the environment — and the environment, for everything
    else in this project, includes `.env`.

    Leaving that out was not a small gap. Four seats sat correctly configured
    in `.env` while both the control plane and `halyard doctor` reported none,
    and doctor's report of nothing configured was the only sign of it.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def from_environment(environ: dict[str, str] | None = None) -> list[Seat]:
    """Every configured seat, newest style first, old style as a fallback.

        HALYARD_SEATS=nav,drv,xnav,xdrv
        HALYARD_SEAT_NAV=runtime=claude-code session=alpha-engine-navigator chat=-1001
        HALYARD_SEAT_XDRV=runtime=codex session=my-codex-driver chat=-1004:12

    A configuration written before any of this existed still works and still
    means the same thing: two seats, both Claude Code, one per role.
    """
    # A real environment variable wins over the file, which is how every other
    # setting in this project behaves.
    env = dict(environ) if environ is not None else {**_dotenv(Path(".env")), **os.environ}

    listed = [label.strip() for label in (env.get("HALYARD_SEATS") or "").split(",")]
    labels = [label for label in listed if label]
    if labels:
        seats = []
        for label in labels:
            key = f"HALYARD_SEAT_{label.upper().replace('-', '_')}"
            spec = env.get(key)
            if spec is None:
                raise ValueError(f"HALYARD_SEATS names {label!r} but {key} is not set")
            seats.append(_parse_seat(label, spec))
        return seats

    # The shape this project had before a second runtime existed.
    legacy = []
    for label, role, session_key, chat_key, runtime_key in (
        (
            "navigator",
            Role.NAVIGATOR,
            "HALYARD_NAVIGATOR_SESSION",
            "TELEGRAM_NAVIGATOR_CHAT_ID",
            "HALYARD_NAVIGATOR_RUNTIME",
        ),
        (
            "driver",
            Role.DRIVER,
            "HALYARD_DRIVER_SESSION",
            "TELEGRAM_DRIVER_CHAT_ID",
            "HALYARD_DRIVER_RUNTIME",
        ),
    ):
        session = env.get(session_key)
        chat = env.get(chat_key)
        if not session and not chat:
            continue
        legacy.append(
            Seat(
                label=label,
                runtime=(env.get(runtime_key) or "claude-code").strip().lower(),
                session=session,
                chat=chat,
                role=role,
            )
        )
    return legacy


def find(seats: list[Seat], label: str) -> Seat | None:
    """A seat by label, case-insensitively — it is typed by hand on a phone."""
    wanted = label.strip().casefold()
    for seat in seats:
        if seat.label.casefold() == wanted:
            return seat
    return None


def for_chat(seats: list[Seat], chat_id: str) -> Seat | None:
    """The seat that owns a chat, if any owns it.

    A chat id may carry a topic, so `-100123` and `-100123:7` are different
    destinations that share a chat. Matching the whole string keeps two seats
    in one group's topics apart.
    """
    for seat in seats:
        if seat.chat and (seat.chat == chat_id or seat.chat.split(":")[0] == chat_id):
            return seat
    return None
