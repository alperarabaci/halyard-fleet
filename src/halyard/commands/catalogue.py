"""Which commands a project offers.

Named in `halyard.yaml`, under the project they belong to:

    projects:
      alpha-engine:
        commands:
          test-all: make test-all
          bootstrap: make bootstrap-up
          cleanup: make cleanup-merged-branches

A name and a line rather than a bare list, for two reasons. Telegram gives a
button 64 bytes of callback data, which a real command line will not fit — so
the button carries the name and Halyard keeps the command. And `cleanup` is
what somebody reads on a phone; `make cleanup-merged-branches` is what they
would have to read past.

Nothing is offered by default. These run whatever they are given on the machine
the control plane is on, so the list is exactly what somebody wrote down and
never a guess about what a project probably supports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: A name has to survive Telegram's callback budget with the prefix in front of
#: it, and be readable on a phone. Anything longer is refused with a reason
#: rather than silently dropped from the list.
NAME_LIMIT = 32


@dataclass(frozen=True)
class Command:
    """One thing a project can be asked to do."""

    name: str
    line: str


def offered(commands: dict[str, str] | None) -> list[Command]:
    """The commands a project lists, in the order they were written.

    A malformed entry is skipped with a warning rather than raised. This is a
    convenience; nothing about it is worth refusing to start the control plane
    over, and the rest of the list still works.
    """
    found: list[Command] = []
    for name, line in (commands or {}).items():
        name, line = str(name).strip(), str(line or "").strip()
        if not name or not line:
            logger.warning("Ignoring command %r: it has no name or no command line", name)
            continue
        if len(name) > NAME_LIMIT:
            logger.warning("Ignoring command %r: names are at most %d characters", name, NAME_LIMIT)
            continue
        found.append(Command(name=name, line=line))
    return found


def resolve(commands: dict[str, str] | None, typed: str) -> Command | None:
    """The command somebody named, or None."""
    wanted = (typed or "").strip().lower()
    for command in offered(commands):
        if command.name.lower() == wanted:
            return command
    return None
