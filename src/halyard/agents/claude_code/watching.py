"""What Claude Code writes down and never says out loud.

A turn that hits an API error mid-way — a 529 overloaded, a usage limit reached
in the middle — does not fire `Stop`, because from the runtime's side the turn
did not *finish responding*, it broke. So the reply relay never runs, and
somebody away from the desk sees a session that has simply gone quiet.

The transcript is the one place it is always recorded, as an ordinary assistant
entry carrying `"isApiErrorMessage": true` — measured on a live session, see
`docs/session-io-notes.md`.

This module is the Claude-shaped half of that: where the files are, how one is
named, and what in it is worth a message. Core does the polling, the byte
offsets, the gate and the not-saying-it-twice, and knows none of the above.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from halyard.agents.spec import Alert, Watching

#: How much of the error text to carry onto a phone.
TEXT_LIMIT = 300


def home() -> Path:
    """Where Claude Code keeps its transcripts."""
    return Path.home() / ".claude"


def transcript(session_id: str, root: Path) -> Path | None:
    """This session's transcript, by id: `<session_id>.jsonl`, somewhere below.

    Searched rather than computed. The directory beneath is a mangled form of
    the project path — `halyard-fleet` and `halyard/fleet` encode identically —
    so building it is guesswork, while the filename is the id exactly. Measured
    across ninety transcripts on one machine: all of them.
    """
    try:
        for found in root.glob(f"**/{session_id}.jsonl"):
            if found.is_file():
                return found
    except OSError:
        return None
    return None


def alerts(lines: Iterable[str], seen: set[str]) -> list[Alert]:
    """API errors in these lines that have not been reported yet.

    Forgiving of everything: a line that is not JSON, an entry that is not an
    error, a shape that has changed so the flag is gone — all produce nothing
    rather than an exception. A missed alert is a missed alert; a raise would be
    this taking a session down with it.
    """
    found: list[Alert] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or not entry.get("isApiErrorMessage"):
            continue
        uuid = entry.get("uuid")
        key = uuid if isinstance(uuid, str) else _text_of(entry)
        if key in seen:
            continue
        found.append(Alert(key=key, text=f"stopped on a server error:\n\n{_text_of(entry)}"))
    return found


def _text_of(entry: dict) -> str:
    """The human-readable error, from the entry's own content or its status."""
    message = entry.get("message")
    if isinstance(message, dict):
        parts = [
            block["text"]
            for block in message.get("content") or []
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        joined = " ".join(part.strip() for part in parts if part.strip())
        if joined:
            return joined[:TEXT_LIMIT]
    status = entry.get("apiErrorStatus")
    return f"Server error{f' ({status})' if status else ''}."


WATCHING = Watching(home=home(), transcript=transcript, alerts=alerts)
