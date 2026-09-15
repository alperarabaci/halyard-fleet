"""How many times a handoff has gone for one piece of work.

A reply sent back comes round again, and nothing said so. alpha-engine#361's
review went three times on 15 September, each time with the whole review text
in front as though it were the first — and the reviewer, told nothing of what it
had found before, found something new each time until somebody cut it off. So a
handoff counts its rounds for the work item its branch names: the envelope says
`Round: 2/2`, and a handoff with a `followup_prompt:` sends that from the second
round on, with the answer the seat gave to the round before.

**A round is a message that arrived.** It is counted when the seat's session
takes it, so a press that reached nobody is not a round, and pressing again is
the same round rather than the next.

**The work, not the prompt's version.** A prompt edited between rounds is the
same work going round again, and the count carries on.

**Kept on this machine**, beside the database, in the shape `last_said` has: a
handful of values that must survive a restart and are not worth a schema. The
machines work on different things, so a work item that moves to another starts
again there.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from halyard.tasks.branches import number_of

logger = logging.getLogger(__name__)

#: How many rounds a handoff is expected to take: the first, and one more with
#: whatever the project wrote for going round again. Past it the count goes on —
#: `3/2` — because every round is somebody pressing a button, and an envelope
#: saying so is worth more than a refusal.
EXPECTED = 2

#: How many pieces of work to remember. A machine works through a few a day;
#: bounded so the file cannot grow without end.
WORKS = 200


@dataclass(frozen=True)
class Round:
    """One time a handoff reached the seat it was for."""

    at: datetime
    #: The seat's label. The answer to this round is what that seat says next.
    to: str


def work_of(branch: str | None, project: str) -> str | None:
    """What rounds are counted against: the work item the branch names —
    `alpha-engine#361` — or the branch itself when its name has no number.

    None on a detached head, where there is nothing to count against.
    """
    if not branch:
        return None
    number = number_of(branch)
    return f"{project}#{number}" if number is not None else branch


def shown(number: int) -> str:
    """A round the way the envelope and the chat say it: `2/2`."""
    return f"{number}/{EXPECTED}"


def _load(where: Path) -> dict:
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def taken(where: Path, work: str, handoff: str) -> list[Round]:
    """The rounds this handoff has had for this work, oldest first."""
    handoffs = _load(where).get(work)
    entries = handoffs.get(handoff) if isinstance(handoffs, dict) else None
    found: list[Round] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            at = datetime.fromisoformat(str(entry.get("at")))
        except (TypeError, ValueError):
            continue
        found.append(
            Round(at=at if at.tzinfo else at.replace(tzinfo=UTC), to=str(entry.get("to") or ""))
        )
    return found


def record(where: Path, work: str, handoff: str, *, to: str, now: datetime | None = None) -> int:
    """Note that a round reached its seat, and say which round it was.

    Best-effort and total, as `last_said.remember` is: this runs on the path
    that delivered the message, and a file that cannot be written costs the
    count and nothing else.
    """
    noted = _load(where)
    handoffs = noted.get(work)
    if not isinstance(handoffs, dict):
        handoffs = {}
    entries = handoffs.get(handoff)
    if not isinstance(entries, list):
        entries = []
    entries.append({"at": (now or datetime.now(UTC)).isoformat(), "to": to})
    handoffs[handoff] = entries
    noted[work] = handoffs
    if len(noted) > WORKS:
        # Oldest out, by the last round each piece of work had — the stored
        # timestamps rather than insertion order, which a rewritten file does
        # not keep.
        noted = dict(sorted(noted.items(), key=_latest, reverse=True)[:WORKS])
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(noted), encoding="utf-8")
    except OSError as unwritable:
        logger.warning("Could not count a round of %s for %s: %s", handoff, work, unwritable)
    return len(entries)


def _latest(item: tuple[str, object]) -> str:
    """When a piece of work last had a round, as stored."""
    _, handoffs = item
    if not isinstance(handoffs, dict):
        return ""
    return max(
        (
            str(entry.get("at") or "")
            for entries in handoffs.values()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, dict)
        ),
        default="",
    )
