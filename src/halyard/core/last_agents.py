"""The agent each role last worked with, per project, so a card can offer it first.

A project with a driver per runtime made every card that asks which driver a
list to search, when the answer was almost always the one used last. Halyard
knows which that was: it delivered the work, or relayed the reply. So this
keeps one name per role, per project, rewritten as it changes. Nothing is
chosen from it — a card puts it first, in colour, and a tap on another agent
makes that one the new first.

Counted whichever way the work went: a message delivered into the agent's
session — typed in its chat, a transition, a workflow step, `/to`, `/forward`
— or the agent's own reply arriving, from a turn started at the desk as much as
one sent from a phone. Halyard's own turns are not agents and never get here.

Best-effort and total, like `last_said`: a file that cannot be read or written
costs the ordering, and nothing else.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Kept per project, beside `last-said.json`.
FILE = "last-agents.json"


def _load(where: Path | None) -> dict:
    if where is None:
        return {}
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def remember(where: Path | None, *, role: str, label: str) -> None:
    """Note that `label` is the agent its `role` last worked with."""
    if where is None or not role or not label:
        return
    noted = _load(where)
    if noted.get(role) == label:
        # Every reply comes through here; the file changes only when the
        # agent does.
        return
    noted[role] = label
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(noted), encoding="utf-8")
    except OSError as unwritable:
        logger.warning("Could not note the last %s in %s: %s", role, where, unwritable)


def recent(where: Path | None) -> dict[str, str]:
    """The agent each role last worked with, by role."""
    return {
        str(role): label for role, label in _load(where).items() if isinstance(label, str) and label
    }
