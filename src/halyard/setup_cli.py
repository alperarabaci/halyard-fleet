"""`halyard init` — build a `halyard.yaml`, wire a project, and check it.

The point is that nobody should have to know the shape of the file before they
can produce one. You are asked what you have — how many seats of each runtime,
which groups they speak in — and the document is assembled from the answers. It
can start from nothing or amend what is already there.

Three things are deliberate.

**The bot token is never echoed.** It is read through `getpass`, so it does not
appear on screen or in shell history — the one credential in this file is also
the one that has already been leaked once, into a log, earlier in this
project's life. It is written to a gitignored file, and not passed on a command
line or printed back.

**The old file is kept, never silently replaced.** A timestamped copy is made
before anything is written, the same rule `halyard wire` follows, and for the
same reason: the file may hold settings this wizard does not manage, and losing
them without a word is the failure worth engineering against.

**Everything unmanaged is carried over.** Log settings, a model default, a
custom bind — keys this wizard does not ask about are read back out of the old
file and written again, so re-running to add a seat cannot quietly drop them.

**One file.** Settings lived in `.env` and seats in `halyard.yaml` for a while,
which is two files describing one machine with no rule written down for which
of them won. There is one now.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from halyard.agents import registry
from halyard.agents.spec import RuntimeSpec
from halyard.core.events import Role
from halyard.core.seats import Seat

#: How many session names to show before trusting the person to type one.
_SESSION_LIST_LIMIT = 12

Ask = Callable[[str, str], str]
Secret = Callable[[str], str]
Say = Callable[[str], None]

#: Keys this wizard owns. Anything else already in the file is carried over
#: untouched, so amending a configuration cannot lose a setting it never asked
#: about.
_MANAGED_PREFIXES = (
    "HALYARD_CHANNEL",
    "HALYARD_BIND",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_AUTHORIZED_USER_IDS",
    "CLAUDE_PROJECT_NAME",
    "HALYARD_SEATS",
    "HALYARD_SEAT_",
)


def _is_managed(key: str) -> bool:
    return any(key == prefix or key.startswith(prefix) for prefix in _MANAGED_PREFIXES)


def _env_label(label: str) -> str:
    """`codex-drv` becomes `CODEX_DRV`, the shape a seat's key takes."""
    return label.upper().replace("-", "_")


def _scalar(value: str) -> str:
    """Quote what YAML would otherwise read as something else.

    Chat ids are the case that matters: `-1001234` is a number to YAML and a
    string to Telegram, and a seat configured with the number routes nowhere.
    """
    if value == "" or re.search(r"""[:#{}\[\]&*!|>%@`'"]|^\s|\s$|^[-\d]""", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def assemble_yaml(
    *,
    token: str,
    default_chat: str,
    authorized_ids: str,
    seats: list[Seat],
    project_name: str | None,
    carried_over: dict[str, str],
    bind: str | None = None,
) -> str:
    """Turn answers into a `halyard.yaml`, carrying unmanaged settings along.

    Pure on purpose: every decision about what the file says is made here,
    where it can be checked without a terminal in the way.

    **One file.** Settings lived in `.env` and seats in `halyard.yaml` for a
    while, which meant two files describing one machine and nothing written
    down about which of them won. Anything this wizard does not ask about is
    read back out of the old document and written again, so amending one answer
    cannot quietly drop a setting.
    """
    settings: dict[str, str] = {"HALYARD_CHANNEL": "telegram"}
    if bind:
        settings["HALYARD_BIND"] = bind
    if project_name:
        settings["CLAUDE_PROJECT_NAME"] = project_name
    settings["TELEGRAM_BOT_TOKEN"] = token
    settings["TELEGRAM_CHAT_ID"] = default_chat
    settings["TELEGRAM_AUTHORIZED_USER_IDS"] = authorized_ids
    for key, value in carried_over.items():
        if not _is_managed(key):
            settings[key] = value

    lines = [
        "# Written by `halyard init`. Re-run it to amend; it keeps a backup first.",
        "",
        "settings:",
    ]
    lines += [f"  {key}: {_scalar(value)}" for key, value in settings.items()]

    lines += ["", "projects:", f"  {project_name or 'a-project'}:"]
    if seats:
        lines.append("    seats:")
        for seat in seats:
            lines.append(f"      {seat.label}:")
            lines.append(f"        runtime: {seat.runtime}")
            if seat.session:
                lines.append(f"        session: {_scalar(seat.session)}")
            if seat.chat:
                lines.append(f"        chat: {_scalar(seat.chat)}")
            if seat.role:
                lines.append(f"        role: {seat.role.value}")
    else:
        lines.append("    seats: {}")

    return "\n".join(lines) + "\n"


def _read_existing(path: Path) -> dict[str, str]:
    """The old document's `settings:` block, for defaults and for carrying over.

    A file the wizard cannot parse offers no defaults rather than raising: the
    goal is to be able to run this against a hand-edited file, not to police it.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    block = document.get("settings") if isinstance(document, dict) else None
    return (
        {str(k): "" if v is None else str(v) for k, v in block.items()}
        if isinstance(block, dict)
        else {}
    )


def _back_up(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def _default_ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    answer = input(shown).strip()
    return answer or default


def _existing_seats(path: Path) -> list[Seat]:
    """The seats already in the file, so re-running can default to them.

    Read from the document's `projects:` block. Reading them from the settings
    block instead — which is what the environment dialect would have made this
    do — finds nothing, and finding nothing here means a re-run that presses
    Enter through every question deletes every seat. That has happened once
    already, from a count that defaulted to zero.
    """
    try:
        from halyard.core.config_file import projects_from_yaml

        described = projects_from_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        # A configuration this wizard cannot parse is one it should not refuse
        # to run against; it simply offers no defaults from it.
        return []
    return [seat for project in described for seat in project.seats]


def _collect_seats(path: Path, ask: Ask, say: Say) -> list[Seat]:
    """Walk through the seats, one runtime at a time.

    Sessions the machine can see are offered by name, because the alternative —
    copying a UUID or a thread title by hand — is the step this wizard exists to
    remove. Reading them is best-effort: a missing CLI means you type the name,
    not that the wizard stops.

    **Every answer defaults to what is already configured.** Re-running this to
    change one thing and pressing Enter through the rest must leave the seats
    where they were. Defaulting the count to zero instead meant that walking
    through with Enter deleted every seat — recoverable from the backup, but
    only by somebody who noticed, and nothing said a word.
    """
    configured = _existing_seats(path)
    seats: list[Seat] = []
    for spec in registry.discover().values():
        runtime, human = spec.name, spec.human
        current = [seat for seat in configured if seat.runtime == runtime]
        available = _known_sessions(spec)
        if available:
            # Newest first, and only a handful. The full list runs to dozens of
            # auto-titled scratch sessions, which buries the named seats it is
            # here to help you pick — and a seat name can still be typed by hand.
            say(f"\n{human} sessions this machine can see (newest first):")
            for name in available[:_SESSION_LIST_LIMIT]:
                say(f"  · {name}")
            if len(available) > _SESSION_LIST_LIMIT:
                say(f"  … and {len(available) - _SESSION_LIST_LIMIT} more")
        count = _to_int(ask(f"\nHow many {human} seats?", str(len(current))))
        for index in range(count):
            say(f"\n  {human} seat {index + 1}:")
            # What this seat already is, if it already is anything. Falling back
            # to a session the machine can see only when there is nothing to
            # keep — an existing seat's own values always win over a guess.
            was = current[index] if index < len(current) else None
            label = ask(
                "    label (short, typed on a phone)",
                was.label if was else f"{spec.prefix}{index + 1}",
            )
            session = ask(
                "    session name",
                (was.session if was and was.session else "")
                or (available[index] if index < len(available) else ""),
            )
            chat = ask("    chat id (blank = reachable by name only)", was.chat if was else "")
            role = ask(
                "    role (navigator / driver / blank)",
                was.role.value if was and was.role else "",
            )
            seats.append(
                Seat(
                    label=label,
                    runtime=runtime,
                    session=session or None,
                    chat=chat or None,
                    role=Role(role.lower()) if role.strip() else None,
                )
            )
    return seats


def _known_sessions(spec: RuntimeSpec) -> list[str]:
    """The names this runtime can see, offered as suggestions.

    Only sessions somebody named. A generated title gets rewritten by the agent
    without warning, so a seat pointed at one stops routing on a day nobody
    touched the configuration.

    Failure is silence: a runtime whose store cannot be read costs you its
    suggestions, and a name can always be typed by hand.
    """
    try:
        return [ref.name for ref in spec.list_sessions()]
    except Exception:
        return []


def _to_int(value: str) -> int:
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return 0


def run(
    *,
    env_path: Path | None = None,
    ask: Ask = _default_ask,
    secret: Secret = getpass.getpass,
    say: Say = print,
    now: str | None = None,
) -> int:
    """Ask, assemble, back up, write — then offer to wire and to check."""
    path = env_path or Path("halyard.yaml")
    existing = _read_existing(path)

    say("This writes halyard.yaml, and can wire the project and check it afterwards.")
    if existing:
        say(f"Found {path}; its values are the defaults below, and it will be backed up.\n")

    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    prompt = "Telegram bot token (hidden; blank keeps the current one)"
    entered = secret(f"{prompt}: ").strip()
    token = entered or token
    if not token:
        say("No bot token, so nothing to write. Nothing was changed.")
        return 1

    default_chat = ask(
        "Default chat id (where anything unrouted lands)",
        existing.get("TELEGRAM_CHAT_ID", ""),
    )
    authorized = ask(
        "Authorized Telegram user ids (comma separated)",
        existing.get("TELEGRAM_AUTHORIZED_USER_IDS", ""),
    )
    project_name = ask("Project name shown on cards", existing.get("CLAUDE_PROJECT_NAME", ""))

    seats = _collect_seats(path, ask, say)

    content = assemble_yaml(
        token=token,
        default_chat=default_chat,
        authorized_ids=authorized,
        seats=seats,
        project_name=project_name or None,
        carried_over=existing,
        bind=existing.get("HALYARD_BIND"),
    )

    stamp = now or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _back_up(path, stamp)
    path.write_text(content, encoding="utf-8")
    say(f"\nWrote {path}" + (f" (previous kept at {backup})" if backup else ""))
    # Never the token — the whole point of getpass is that it does not surface.
    say(f"  {len(seats)} seat(s): " + ", ".join(f"{s.label}/{s.runtime}" for s in seats))

    _offer_wire(ask, say)
    _offer_doctor(ask, say)
    return 0


def _offer_wire(ask: Ask, say: Say) -> None:
    where = ask("\nWire a project now? Give its path, or blank to skip", "")
    if not where.strip():
        return
    directory = Path(where).expanduser()
    if not directory.is_dir():
        say(f"  {directory} is not a directory; skipping.")
        return
    from halyard import wiring

    wiring.wire(directory.resolve())


def _offer_doctor(ask: Ask, say: Say) -> None:
    if ask("\nRun `halyard doctor` now? (y/n)", "y").strip().lower() not in ("y", "yes"):
        return
    from halyard.doctor import run as doctor_run

    say("")
    doctor_run()
