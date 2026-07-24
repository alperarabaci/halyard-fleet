"""`halyard init` — build a `.env`, wire a project, and check it, in one sitting.

The point is that nobody should have to know the shape of `.env` before they
can produce one. You are asked what you have — how many Claude seats, how many
Codex seats, which groups they speak in — and the file is assembled from the
answers. It can start from nothing or amend what is already there.

Three things are deliberate.

**The bot token is never echoed.** It is read through `getpass`, so it does not
appear on screen or in shell history — the one credential in this file is also
the one that has already been leaked once, into a log, earlier in this
project's life. It is written to `.env` because that is what `.env` is; it is
not passed on a command line or printed back.

**The old file is kept, never silently replaced.** A timestamped copy is made
before anything is written, the same rule `halyard wire` follows, and for the
same reason: the file may hold settings this wizard does not manage, and losing
them without a word is the failure worth engineering against.

**Everything unmanaged is carried over.** Log settings, a model default, a
custom bind — keys this wizard does not ask about are read back out of the old
file and written again, so re-running to add a seat cannot quietly drop them.

YAML, and more than one project in one file, come next. This writes the flat
`.env` that the control plane already reads, which is the thing that has to
work first.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from halyard.core.events import Role
from halyard.core.seats import Seat

#: How many session names to show before trusting the person to type one.
_SESSION_LIST_LIMIT = 12

Ask = Callable[[str, str], str]
Secret = Callable[[str], str]
Say = Callable[[str], None]

#: Keys this wizard owns. Anything else in an existing `.env` is carried over
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


def assemble_env(
    *,
    token: str,
    default_chat: str,
    authorized_ids: str,
    seats: list[Seat],
    project_name: str | None,
    carried_over: dict[str, str],
    bind: str | None = None,
) -> str:
    """Turn answers into the text of a `.env`, carrying unmanaged keys along.

    Pure on purpose: every decision about what the file says is made here,
    where it can be checked without a terminal in the way.
    """
    lines = [
        "# Written by `halyard init`. Re-run it to amend; it keeps a backup first.",
        "",
        "HALYARD_CHANNEL=telegram",
    ]
    if bind:
        lines.append(f"HALYARD_BIND={bind}")
    if project_name:
        lines.append(f"CLAUDE_PROJECT_NAME={project_name}")
    lines += [
        "",
        "# One bot covers every group. The token is secret; the ids are not.",
        f"TELEGRAM_BOT_TOKEN={token}",
        f"TELEGRAM_CHAT_ID={default_chat}",
        f"TELEGRAM_AUTHORIZED_USER_IDS={authorized_ids}",
    ]

    if seats:
        lines += ["", "# Seats. Each is a place a message can go.", ""]
        lines.append("HALYARD_SEATS=" + ",".join(seat.label for seat in seats))
        for seat in seats:
            parts = [f"runtime={seat.runtime}"]
            if seat.session:
                parts.append(f"session={seat.session}")
            if seat.chat:
                parts.append(f"chat={seat.chat}")
            if seat.role:
                parts.append(f"role={seat.role.value}")
            lines.append(f"HALYARD_SEAT_{_env_label(seat.label)}=" + " ".join(parts))

    leftover = {k: v for k, v in carried_over.items() if not _is_managed(k)}
    if leftover:
        lines += ["", "# Carried over from the previous file, unchanged.", ""]
        lines += [f"{key}={value}" for key, value in leftover.items()]

    return "\n".join(lines) + "\n"


def _read_existing(path: Path) -> dict[str, str]:
    """The old `.env` as key→value, for defaults and for carrying over.

    A line the wizard cannot parse is simply skipped rather than raising: the
    goal is to be able to run this against a hand-edited file, not to police it.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


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


def _existing_seats(existing: dict[str, str]) -> list[Seat]:
    """The seats already configured, so re-running can default to them."""
    try:
        from halyard.core.seats import from_environment

        return from_environment(existing)
    except Exception:
        # A configuration this wizard cannot parse is one it should not refuse
        # to run against; it simply offers no defaults from it.
        return []


def _collect_seats(existing: dict[str, str], ask: Ask, say: Say) -> list[Seat]:
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
    configured = _existing_seats(existing)
    seats: list[Seat] = []
    for runtime, human in (("claude-code", "Claude Code"), ("codex", "Codex")):
        current = [seat for seat in configured if seat.runtime == runtime]
        available = _known_sessions(runtime)
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
                was.label if was else f"{_short(runtime)}{index + 1}",
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


def _known_sessions(runtime: str) -> list[str]:
    try:
        if runtime == "codex":
            from halyard.agents.codex import list_named_sessions

            return [name for name, _, _ in list_named_sessions()]
        from halyard.agents.claude_code.sessions import list_named_sessions

        return [name for name, _, _ in list_named_sessions()]
    except Exception:
        return []


def _short(runtime: str) -> str:
    return "x" if runtime == "codex" else ""


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
    path = env_path or Path(".env")
    existing = _read_existing(path)

    say("This writes .env, and can wire the project and check it afterwards.")
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

    seats = _collect_seats(existing, ask, say)

    content = assemble_env(
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
