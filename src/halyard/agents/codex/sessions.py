"""Finding a Codex session by the name a person gave it.

The same job as the Claude Code module beside this one, and it has to be done
differently, because Codex keeps the two halves in two places. Measured against
CLI `0.145.0`:

- The **name** lives only in `~/.codex/session_index.jsonl`, appended as
  `{"id": ..., "thread_name": ..., "updated_at": ...}`. Nothing in the rollout
  transcript carries it, so a transcript alone cannot tell you what a session is
  called.
- The **directory, model and effort** live in the rollout transcript, under
  `session_meta` for the first and `turn_context` for the ones that change.

There is also `~/.codex/state_5.sqlite`, whose `threads` table holds all of it
in one row. It is not read here on purpose: the `5` is a schema version, and
building on an internal database that announces its own churn is how you get a
tool that breaks on somebody else's release. The two files above are the same
kind of source Halyard already reads for Claude Code.
"""

from __future__ import annotations

import json
import logging
import mmap
from pathlib import Path

from halyard.agents.base import SessionRef

logger = logging.getLogger(__name__)

#: Context records are small. Refuse to materialise an unexpectedly enormous
#: JSON line while walking backwards over tool output; it cannot be one of the
#: records this adapter is looking for.
MAX_CONTEXT_RECORD_BYTES = 2 * 1024 * 1024


def codex_home(root: Path | None = None) -> Path:
    return root or Path.home() / ".codex"


def _index_entries(root: Path | None = None) -> list[dict]:
    """Every line of the session index, oldest first.

    Append-only, so a session renamed twice appears twice and the last entry
    wins. Anything unreadable is skipped rather than raising: a malformed line
    should cost one session, not the whole listing.
    """
    index = codex_home(root) / "session_index.jsonl"
    try:
        raw = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("id"):
            entries.append(record)
    return entries


def _rollout_for(session_id: str, root: Path | None = None) -> Path | None:
    """The transcript belonging to a session id.

    Filed by date under `sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`,
    so it is found by the id in the name rather than by knowing the date.
    """
    sessions = codex_home(root) / "sessions"
    try:
        matches = list(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _context_of(transcript: Path) -> tuple[str | None, str | None, str | None]:
    """The directory, model and effort a session was last running with.

    `turn_context` is written per turn and is therefore current; `session_meta`
    is written at each app attachment and supplies the newest directory when it
    follows the last turn. Walk records backwards rather than reading a fixed
    byte tail: a 283 MB production rollout had 350 ordinary records after its
    latest context, so the previous 256 KiB window contained no context at all
    and incorrectly reported that the session had no working directory.

    The file is memory-mapped, not read into memory. Oversized tool-output lines
    are skipped by length before slicing, and the scan stops at the first
    `turn_context` after collecting any newer `session_meta` values.
    """
    try:
        handle = transcript.open("rb")
    except OSError:
        return None, None, None

    cwd = model = effort = None
    try:
        with handle:
            if transcript.stat().st_size == 0:
                return None, None, None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                end = len(data)
                if data[-1:] == b"\n":
                    end -= 1

                while end >= 0:
                    newline = data.rfind(b"\n", 0, end)
                    start = newline + 1
                    if end - start <= MAX_CONTEXT_RECORD_BYTES:
                        try:
                            record = json.loads(data[start:end])
                        except ValueError:
                            record = None
                        payload = record.get("payload") if isinstance(record, dict) else None
                        if isinstance(payload, dict) and record.get("type") in (
                            "session_meta",
                            "turn_context",
                        ):
                            cwd = cwd or payload.get("cwd")
                            model = model or payload.get("model")
                            effort = (
                                effort or payload.get("effort") or payload.get("reasoning_effort")
                            )
                            if record.get("type") == "turn_context":
                                break
                    if newline < 0:
                        break
                    end = newline
    except (OSError, ValueError):
        return None, None, None
    return cwd, model, effort


def describe(session_id: str, name: str, *, root: Path | None = None) -> SessionRef:
    """Everything known about one session, with whatever is missing left None."""
    transcript = _rollout_for(session_id, root)
    cwd, model, effort = _context_of(transcript) if transcript else (None, None, None)
    return SessionRef(session_id=session_id, name=name, cwd=cwd, model=model, effort=effort)


def find_session(name: str, *, root: Path | None = None) -> SessionRef | None:
    """Resolve a thread name — or a raw session id — to a session.

    Both are accepted because `codex exec resume` accepts both, and somebody
    holding an id should not be told to go and find a name for it first.
    """
    wanted = name.strip()
    if not wanted:
        return None

    latest: dict[str, str] = {}
    for entry in _index_entries(root):
        thread_name = entry.get("thread_name")
        if thread_name:
            # Append-only: a later line for the same id supersedes an earlier
            # one, and a name reused on a new session moves to the new id.
            latest[str(thread_name)] = str(entry["id"])

    if wanted in latest:
        return describe(latest[wanted], wanted, root=root)

    folded = {key.casefold(): value for key, value in latest.items()}
    if wanted.casefold() in folded:
        return describe(folded[wanted.casefold()], wanted, root=root)

    if _rollout_for(wanted, root) is not None:
        return describe(wanted, wanted, root=root)
    return None


def list_named_sessions(*, root: Path | None = None) -> list[tuple[str, str, str]]:
    """Every named session as (name, session_id, last updated), newest first."""
    latest: dict[str, tuple[str, str]] = {}
    for entry in _index_entries(root):
        thread_name = entry.get("thread_name")
        if thread_name:
            latest[str(thread_name)] = (str(entry["id"]), str(entry.get("updated_at") or ""))
    return sorted(
        ((name, sid, when) for name, (sid, when) in latest.items()),
        key=lambda item: item[2],
        reverse=True,
    )
