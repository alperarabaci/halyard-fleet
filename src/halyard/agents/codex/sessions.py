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
from datetime import datetime
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


def rollout_for(session_id: str, root: Path | None = None) -> Path | None:
    """The transcript a session id is being written to now.

    Filed by date under `sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`,
    so it is found by the id in the name rather than by knowing the date.

    The Desktop app moves a thread on to a new file every so often, named
    `rollout-<timestamp>-<id>_<another id>.jsonl`, each starting afresh — so
    the one being written is the newest. Looking only for the plain name found
    the oldest, with the model and effort of days before, and found nothing at
    all for a thread with no plain file left: no working directory, so nothing
    could be delivered to it. Measured on a second machine: Codex had
    compressed that thread's older rollouts, its first among them, to
    `.jsonl.zst`; the newer, moved-on ones were still plain `.jsonl`.
    """
    sessions = codex_home(root) / "sessions"
    try:
        found = [
            path
            for pattern in (f"rollout-*-{session_id}.jsonl", f"rollout-*-{session_id}_*.jsonl")
            for path in sessions.glob(f"*/*/*/{pattern}")
            if path.is_file()
        ]
        return max(found, key=lambda path: path.stat().st_mtime) if found else None
    except OSError:
        return None


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


def describe(
    session_id: str, name: str, *, root: Path | None = None, transcript: Path | None = None
) -> SessionRef:
    """Everything known about one session, with whatever is missing left None.
    `transcript` is its rollout, when the caller has found it already."""
    transcript = transcript or rollout_for(session_id, root)
    cwd, model, effort = _context_of(transcript) if transcript else (None, None, None)
    return SessionRef(session_id=session_id, name=name, cwd=cwd, model=model, effort=effort)


def _current(root: Path | None = None) -> dict[str, dict]:
    """Each thread's index line as it stands now: the last one for its id.

    A thread renamed keeps its earlier lines, so a name is only a thread's
    while its latest line says it. Read line by line into a name → id map, a
    renamed thread went on answering to its old name too: a fork renamed
    `… (2)` after it was made could keep the name it was made under, and the
    agent's messages with it.
    """
    return _latest_per_id(_index_entries(root))


def _latest_per_id(entries: list[dict]) -> dict[str, dict]:
    current: dict[str, dict] = {}
    for entry in entries:
        current[str(entry["id"])] = entry
    return current


def _named(name: str, root: Path | None = None) -> list[tuple[str, dict, Path | None]]:
    """The threads called `name` now, with each one's index line and the rollout
    it is being written to — None for a thread whose transcript is gone.

    Exactly as written, or failing that the same but for case, because the name
    is copied by hand.
    """
    wanted = name.strip()
    current = _current(root)
    exact = [
        (session_id, entry)
        for session_id, entry in current.items()
        if str(entry.get("thread_name") or "") == wanted
    ]
    if not exact:
        folded = wanted.casefold()
        exact = [
            (session_id, entry)
            for session_id, entry in current.items()
            if str(entry.get("thread_name") or "").casefold() == folded
        ]
    return [(session_id, entry, rollout_for(session_id, root)) for session_id, entry in exact]


def _forked_from(transcript: Path) -> str | None:
    """The thread a rollout was forked from, as its first record says. Codex
    Desktop wrote `forked_from_id` into the `session_meta` of a side chat."""
    try:
        with transcript.open("rb") as handle:
            first = handle.readline(MAX_CONTEXT_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(first) > MAX_CONTEXT_RECORD_BYTES:
        return None
    try:
        record = json.loads(first)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    parent = payload.get("forked_from_id") if isinstance(payload, dict) else None
    return str(parent) if parent else None


def _short(session_id: str, others: set[str]) -> str:
    """As much of an id as tells it apart from the others, eight characters at
    least. The ids are UUIDv7, whose first characters are the time it was made,
    so two threads opened a minute apart share them."""
    for length in range(8, len(session_id)):
        prefix = session_id[:length]
        if not any(other != session_id and other.startswith(prefix) for other in others):
            return prefix
    return session_id


def name_clash(name: str, *, root: Path | None = None) -> str | None:
    """Why `name` addresses no one thread: more than one is called it now. None
    when one is, or none.

    Measured on 2026-11-05: a side chat opened by a stray click took its main
    thread's name and no ` (2)`, and a workflow step went into it — answered
    where nobody was looking, in a chat the Desktop app did not show. Two
    threads under one name is something only a person can put right, so it is
    said, with what tells the two apart, rather than guessed between. A thread
    whose transcript is gone — deleted, archived — holds no name.
    """
    named = [(session_id, entry, path) for session_id, entry, path in _named(name, root) if path]
    if len(named) < 2:
        return None
    ids = {session_id for session_id, _, _ in named}
    told = []
    for session_id, entry, path in named:
        said = _short(session_id, ids)
        if (at := _as_time(entry.get("updated_at"))) is not None:
            local = at.astimezone()
            said += f", named {local:%b} {local.day} {local:%H:%M}"
        if (parent := _forked_from(path)) in ids:
            said += f", a fork of {_short(parent, ids)}"
        told.append(said)
    return f"{len(named)} Codex threads are named {name.strip()}: " + "; ".join(told)


def find_session(name: str, *, root: Path | None = None) -> SessionRef | None:
    """Resolve a thread name — or a raw session id — to a session.

    Both are accepted because `codex exec resume` accepts both, and somebody
    holding an id should not be told to go and find a name for it first.

    A name is the one thread called it now. Two of them is no session at all —
    `name_clash` says which — rather than the newest, because the newest was a
    side chat opened by accident. A name is handed to a new thread on purpose
    by renaming the one that had it.
    """
    wanted = name.strip()
    if not wanted:
        return None

    named = _named(wanted, root)
    there = [(session_id, path) for session_id, _, path in named if path is not None]
    if len(there) == 1:
        [(session_id, path)] = there
        return describe(session_id, wanted, root=root, transcript=path)
    if there:
        logger.warning("%d Codex threads are named %r; none of them is used", len(there), wanted)
        return None
    if named:
        # Named, with no transcript left to say where it runs: found, so what
        # is refused is refused for that, and not as a name nobody knows.
        return describe(named[-1][0], wanted, root=root)

    if rollout_for(wanted, root) is not None:
        return describe(wanted, wanted, root=root)
    return None


def _as_time(value: object) -> datetime | None:
    """The index's ISO timestamp, or None if it is not one.

    Parsed here rather than handed on as a string. Before this the three
    runtimes each kept "last active" in a different type — two floats and this
    ISO string — behind a signature that promised a float, and nothing caught
    it because the only caller discarded the field.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_named_sessions(*, root: Path | None = None) -> list[SessionRef]:
    """Every named thread, newest first.

    The index carries a name, an id and a timestamp — no directory, which lives
    in the rollout and is not worth opening one per thread just to list them.
    `cwd` is left unset rather than guessed.
    """
    entries = _index_entries(root)
    current = _latest_per_id(entries)
    latest: dict[str, SessionRef] = {}
    for entry in entries:
        thread_name = entry.get("thread_name")
        if not thread_name or current.get(str(entry["id"])) is not entry:
            # Only the name each thread has now: an old one is nobody's.
            continue
        # Append-only, so a later line for the same name supersedes an earlier.
        latest[str(thread_name)] = SessionRef(
            session_id=str(entry["id"]),
            name=str(thread_name),
            cwd=None,
            last_active=_as_time(entry.get("updated_at")),
        )
    return sorted(
        latest.values(),
        key=lambda ref: -(ref.last_active.timestamp() if ref.last_active else 0.0),
    )
