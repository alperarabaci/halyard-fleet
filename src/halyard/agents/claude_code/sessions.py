"""Finding a session by the name it carries in the app.

The registry learns about a session the first time a hook fires for it, which
is enough to *label* traffic but not enough to *address* it: a control plane
that just restarted knows nothing, and telling someone to go run a command
somewhere before they can send a message is not an answer.

A name is addressable without any of that. It is written in the transcript, it
survives restarts on both sides, and it is what the user already configured.

Duplicated in miniature in `bridge/_settings.py`, which is standalone by design
— it runs inside somebody else's process tree and cannot import this package.
Two small readers of one format is the cost of that, and it is the right trade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Titles are rewritten through the last few percent of a transcript, so the
#: tail is enough. Measured at about a millisecond on a 5 MB file.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class SessionRef:
    """Where a session is, and what it is called."""

    session_id: str
    name: str
    #: The directory the session belongs to. Needed because `claude --resume`
    #: looks for a conversation within the current project and finds nothing if
    #: run from somewhere else — which is what a control plane running from its
    #: own repository always is.
    cwd: str | None


def _read_tail(transcript: Path) -> bytes | None:
    try:
        with transcript.open("rb") as handle:
            handle.seek(max(0, transcript.stat().st_size - TRANSCRIPT_TAIL_BYTES))
            return handle.read()
    except OSError:
        return None


def describe(transcript: Path) -> SessionRef | None:
    """Read a session's name and directory out of its transcript.

    The directory is taken from the `cwd` a record carries, not from the
    encoded directory name transcripts are filed under: that encoding replaces
    path separators with dashes and cannot be undone, since a real dash in a
    folder name looks exactly the same.

    A title the user chose wins over one Claude generated — the generated one
    moves with the conversation, and the point here is to be stable.
    """
    tail = _read_tail(transcript)
    if tail is None:
        return None

    custom = generated = cwd = None
    for raw in tail.split(b"\n"):
        if b"-title" not in raw and b'"cwd"' not in raw:
            continue
        try:
            record = json.loads(raw)
        except Exception:
            continue
        if record.get("type") == "custom-title" and record.get("customTitle"):
            custom = str(record["customTitle"])
        elif record.get("type") == "ai-title" and record.get("aiTitle"):
            generated = str(record["aiTitle"])
        if record.get("cwd"):
            cwd = str(record["cwd"])

    name = custom or generated
    return SessionRef(session_id=transcript.stem, name=name, cwd=cwd) if name else None


def title_of(transcript: Path) -> str | None:
    """The name shown on a session, or None if it has none or cannot be read."""
    ref = describe(transcript)
    return ref.name if ref else None


def find_session(name: str, *, root: Path | None = None) -> SessionRef | None:
    """The most recent session carrying `name`.

    Most recent because one named conversation is resumed under a new id every
    time it restarts, and the newest is the one still being worked in. Matched
    case-insensitively and trimmed, because the name is copied by hand.
    """
    wanted = name.strip().casefold()
    if not wanted:
        return None
    directory = root or transcript_root()
    try:
        transcripts = sorted(
            directory.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None

    for transcript in transcripts:
        ref = describe(transcript)
        if ref and ref.name.strip().casefold() == wanted:
            return ref
    return None


def list_named_sessions(*, root: Path | None = None) -> list[tuple[str, str, float]]:
    """Every named session as (name, session_id, last modified), newest first."""
    directory = root or transcript_root()
    seen: dict[str, tuple[str, float]] = {}
    try:
        transcripts = list(directory.glob("*/*.jsonl"))
    except OSError:
        return []
    for transcript in transcripts:
        try:
            modified = transcript.stat().st_mtime
        except OSError:
            continue
        ref = describe(transcript)
        if ref is None:
            continue
        if ref.name not in seen or modified > seen[ref.name][1]:
            seen[ref.name] = (ref.session_id, modified)
    return sorted(
        ((name, sid, mtime) for name, (sid, mtime) in seen.items()),
        key=lambda item: -item[2],
    )
