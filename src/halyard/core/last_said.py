"""The last thing an agent said in each chat, so it can be handed on.

`/forward` needs one fact: what arrived here most recently. Nothing else in
Halyard keeps it. The audit log records that an agent spoke, and deliberately
not what it said — that log is the permanent, append-only record of decisions,
and a conversation is not one.

So this is a small, mutable file beside the database, in the shape
`credentials.py` already established for exactly this kind of thing: a handful
of values that have to survive a restart and are not worth a schema. One entry
per chat, replaced each time, and a restart is the ordinary case rather than a
loss — the thing somebody wants to forward is almost always the thing that just
came in, and holding it only in memory meant a restart threw it away.

**Kept whole.** Telegram splits a long message into several, and what is stored
here is what arrived before the split. Forwarding a fragment of a report would
be worse than forwarding nothing, because it would look complete.

Keyed by chat rather than by seat. `/forward` is asked in a conversation and
means "this one, over there" — which chat it was asked in is the question, and
a chat is what a person is looking at.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Most characters kept per chat. A long report is what somebody wants to hand
#: on, so this is generous — but not unbounded, because the file is rewritten
#: on every message an agent sends and nobody wants a megabyte of it.
LIMIT = 100_000

#: How many chats to remember. One per seat, and a machine has a handful.
#: Bounded so a bot in many groups cannot grow this without limit.
CHATS = 40

#: Past this, what arrived is history rather than something being handed on.
#: Long enough to read a report, walk away and come back to it.
STALE_AFTER_HOURS = 24


@dataclass(frozen=True)
class Said:
    """What was last relayed into a chat."""

    text: str
    at: datetime
    #: The session it came from, so a forward can say where it came from.
    session_id: str | None = None

    def stale(self, now: datetime, hours: int = STALE_AFTER_HOURS) -> bool:
        return (now - self.at).total_seconds() > hours * 3600


def _load(where: Path) -> dict:
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def remember(
    where: Path,
    *,
    chat_id: str,
    text: str,
    session_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Note what an agent just said in this chat.

    Best-effort and total. This is called on the path that delivers an agent's
    reply, and a file that cannot be written must cost the convenience and
    nothing else — a reply that reached somebody is worth more than a note
    about it.
    """
    if not chat_id or not (text or "").strip():
        return
    noted = _load(where)
    noted[str(chat_id)] = {
        "text": text[:LIMIT],
        "at": (now or datetime.now(UTC)).isoformat(),
        "session_id": session_id,
    }
    if len(noted) > CHATS:
        # Oldest out. Sorted on the stored timestamp rather than on insertion
        # order, which a rewritten file does not preserve.
        keep = sorted(noted.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True)
        noted = dict(keep[:CHATS])
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(noted), encoding="utf-8")
    except OSError as unwritable:
        logger.warning("Could not note what was said in %s: %s", chat_id, unwritable)


def last(where: Path, chat_id: str) -> Said | None:
    """What was last said in this chat, or None."""
    entry = _load(where).get(str(chat_id or ""))
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    session = entry.get("session_id")
    return Said(text=text, at=at, session_id=session if isinstance(session, str) else None)
