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
from pathlib import Path

from halyard.agents.base import SessionRef

logger = logging.getLogger(__name__)

#: Enough of a rollout to find the newest `turn_context` without reading a
#: conversation that may be megabytes of tool output.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


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


def _read_tail(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            return handle.read()
    except OSError:
        return None


def _context_of(transcript: Path) -> tuple[str | None, str | None, str | None]:
    """The directory, model and effort a session was last running with.

    `turn_context` is written per turn and is therefore current; `session_meta`
    is written once at the start and is the fallback. Reading only the first
    record would report the model a long conversation began with, which in a
    navigator/driver pair is exactly the thing being checked.
    """
    tail = _read_tail(transcript)
    if tail is None:
        return None, None, None

    cwd = model = effort = None
    for raw in tail.split(b"\n"):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        if record.get("type") in ("session_meta", "turn_context"):
            cwd = payload.get("cwd") or cwd
            model = payload.get("model") or model
            effort = payload.get("effort") or payload.get("reasoning_effort") or effort
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
