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


def _default_runtime() -> str:
    """What a seat is when its configuration did not say.

    Every dialect this project has had defaulted to Claude Code, and the name
    lives in the registry so that moving it does not mean finding it in three
    parsers.
    """
    from halyard.agents import registry

    return registry.DEFAULT


def known_runtimes() -> tuple[str, ...]:
    """What a seat's `runtime:` may say — asked, not listed.

    This used to be a tuple written out here, with a note explaining that
    reading a configuration should not import a CLI wrapper. The note was
    right about the cost and wrong about the trade: a second list of runtime
    names is a second place to edit, and the two drifting apart means a seat
    somebody configured against a runtime the registry has, refused by a
    validator that has not heard of it.

    Imported inside the function so this module still costs nothing to import.
    """
    from halyard.agents import registry

    return registry.names()


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
    #: Which codebase this seat works in. Set by the YAML configuration, where
    #: seats are written underneath the project they belong to; the environment
    #: dialect has no way to say it and leaves it unset, which is honest —
    #: that dialect only ever described one project.
    project: str | None = None

    def __post_init__(self) -> None:
        allowed = known_runtimes()
        if self.runtime not in allowed:
            raise ValueError(
                f"Seat {self.label!r} has runtime {self.runtime!r}. "
                f"Use one of: {', '.join(allowed)}."
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
        runtime=fields.get("runtime", _default_runtime()).lower(),
        session=fields.get("session"),
        chat=fields.get("chat"),
        role=Role(role.lower()) if role else None,
    )


def from_environment(environ: dict[str, str] | None = None) -> list[Seat]:
    """Every configured seat, newest style first, old style as a fallback.

        HALYARD_SEATS=nav,drv,xnav,xdrv
        HALYARD_SEAT_NAV=runtime=claude-code session=alpha-engine-navigator chat=-1001
        HALYARD_SEAT_XDRV=runtime=codex session=my-codex-driver chat=-1004:12

    A configuration written before any of this existed still works and still
    means the same thing: two seats, both Claude Code, one per role.
    """
    # The process environment, and nothing else. There was a `.env` beside
    # this once — read here and by everything else — and the two files it made
    # were the reason one machine needed both edited to change one thing, with
    # nothing written down about which of them won. `halyard.yaml` is the file
    # now; a real environment variable still overrides it, which is how a
    # container passes a token in without writing it to disk.
    env = dict(environ) if environ is not None else dict(os.environ)

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
                runtime=(env.get(runtime_key) or _default_runtime()).strip().lower(),
                session=session,
                chat=chat,
                role=role,
            )
        )
    return legacy


def configured(directory: Path | None = None) -> list[Seat]:
    """Every seat, from whichever dialect describes them.

    One precedence rule, in one place, so the control plane and `doctor` can
    never disagree about what is configured — a disagreement that already
    happened once, when seats were read from `os.environ` here and from `.env`
    everywhere else, and four correct seats produced a control plane holding
    none.

    **YAML wins outright when the file exists; the two are never merged.**
    Merging would answer "which of these two files is this seat from?" with
    "both, partly", and a seat somebody thought they had replaced would still
    be routing. A file that exists and cannot be read raises rather than
    falling back, for the same reason: starting with a configuration nobody
    wrote is worse than not starting.
    """
    from halyard.core.config_file import find_config, load

    if find_config(directory) is not None:
        return load(directory)
    return from_environment()


def find(seats: list[Seat], label: str) -> Seat | None:
    """A seat by label, case-insensitively — it is typed by hand on a phone."""
    wanted = label.strip().casefold()
    for seat in seats:
        if seat.label.casefold() == wanted:
            return seat
    return None


def for_session(seats: list[Seat], runtime: str | None, *identifiers: str | None) -> Seat | None:
    """The seat that owns a session, addressed as `(runtime, session)`.

    **Never by name alone.** Two runtimes can hold the same name at once —
    `alpha-engine-driver` is a Claude Code session *and* an Antigravity
    conversation on this machine, which is the ordinary case rather than a
    contrived one, because a person naming two seats for one job names them the
    same thing. Matching on the name and taking the first hit routes by seat
    order: Antigravity's reply was delivered into the Claude driver's group,
    and nothing about the message said it had gone to the wrong runtime.

    Either identifier is accepted — the readable name or the session id —
    because they fail differently. A name is what you copy out of an app and
    can be changed there without anybody remembering a seat pointed at it; an
    id is unreadable and permanent. What is *not* optional is the runtime.

    `runtime=None` matches on identifier alone, and is only for a caller that
    genuinely cannot know — which is not the same as one that did not ask.
    """
    wanted = {value.strip().casefold() for value in identifiers if value and value.strip()}
    if not wanted:
        return None
    for seat in seats:
        if runtime is not None and seat.runtime != runtime:
            continue
        if seat.session and seat.session.strip().casefold() in wanted:
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
