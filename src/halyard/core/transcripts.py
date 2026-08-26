"""Notice when a turn dies where no hook fires, and say so on the phone.

Some failures leave no event to react to. A turn that hits an API error mid-way
— a 529 overloaded, a usage limit reached in the middle — does not fire `Stop`,
because from Claude Code's side the turn did not *finish responding*, it broke.
So the reply relay never runs, and somebody away from the desk sees nothing:
the session simply goes quiet with a synthetic error message sitting in its
transcript.

The transcript is the one place the failure is always recorded. Claude Code
writes it as an ordinary assistant entry carrying `"isApiErrorMessage": true`
and the human-readable text — measured, see `docs/session-io-notes.md`. There
is nothing to hang a hook on, so this watches the file on a timer instead.

Two rules shape every line here, both asked for directly:

**It must never break the gate.** This is a courier for a nice-to-have alert,
not part of the approval path, so every file read, every parse, every push is
wrapped and any failure is swallowed. A transcript that has moved, a format that
has changed, a permission that was revoked — each makes this quietly do nothing,
never raise.

**It must stay cheap.** Only sessions a hook has actually mentioned are watched,
the poll reads only the bytes appended since last time, an idle session is
dropped, and the path is taken from the hook payload rather than guessed from a
directory layout that is not ours to depend on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os.path
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.core.events import Role
from halyard.core.gate import Gate

logger = logging.getLogger(__name__)

#: How often the watched transcripts are checked. Loose on purpose: a stalled
#: turn is not an emergency measured in seconds, and a tight loop over files is
#: the kind of weight this was explicitly asked not to become.
DEFAULT_POLL_SECONDS = 15.0

#: A session nothing has mentioned for this long is dropped. Errors land within
#: seconds of the last command, so half an hour is generous; past it, a quiet
#: session is not about to surprise anybody.
DEFAULT_IDLE_TTL = timedelta(minutes=30)

#: Most bytes to read from one transcript per poll. A backlog is worked through
#: over several polls rather than in one blocking read.
MAX_READ_BYTES = 512 * 1024

#: How many recently-seen entry ids to remember per session, so a file that is
#: replaced and re-read does not report the same error twice. Bounded so a long
#: session cannot grow this without limit.
MAX_SEEN = 200

#: Only this runtime writes a transcript in the shape read below.
CLAUDE_CODE = "claude-code"

#: Where the runtimes keep their transcripts. A path that resolves outside all
#: of these is not a transcript and is not opened.
#:
#: The path itself still comes from the hook payload rather than being computed
#: — a layout that moves inside one of these directories keeps working, which
#: was the point of not building the path here. What this adds is a boundary:
#: the payload arrives over HTTP, and anything that can reach the control plane
#: could otherwise name `~/.ssh/id_rsa` and have its contents read, summarised
#: by a model and handed back at the next session start. Found by CodeQL, and
#: it was right.
TRANSCRIPT_ROOTS = (
    Path.home() / ".claude",
    Path.home() / ".codex",
    Path.home() / ".gemini",
)


def inside_a_transcript_root(
    path: str | Path, roots: tuple[Path, ...] | None = None
) -> Path | None:
    """The path, resolved, if it sits inside a runtime's own transcript store.

    None for everything else, which every caller turns into "read nothing".

    **The containment check happens on the string, before anything touches the
    filesystem.** That ordering is the point: `resolve()` follows symlinks, so
    calling it on an unchecked path is itself a filesystem operation driven by
    whatever the payload said. CodeQL flagged exactly that and was right twice
    — once for the read, and again for this.

    Symlinks are still refused, by resolving *after* the string check and
    requiring the answer to be inside the same root. A link planted inside a
    transcript directory cannot point out of one.
    """
    if not path:
        return None

    candidate = os.path.normpath(os.path.expanduser(str(path)))
    if not os.path.isabs(candidate):
        # Transcript paths arrive absolute. A relative one would be measured
        # against this process's working directory, which is a fact about the
        # service and not about the file.
        return None

    for root in roots or TRANSCRIPT_ROOTS:
        prefix = os.path.normpath(os.path.expanduser(str(root)))
        if candidate != prefix and not candidate.startswith(prefix + os.sep):
            continue
        # Inside a known root by name. Only now is it worth asking the
        # filesystem, and the answer has to stay inside the same root.
        try:
            resolved = Path(candidate).resolve()
            resolved.relative_to(Path(prefix).resolve())
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    logger.warning("Refusing to read %s: it is not inside a runtime's transcript directory", path)
    return None


#: How much of the error text to carry onto the phone.
TEXT_LIMIT = 300


def find_api_errors(lines: Iterable[str], seen: set[str]) -> list[tuple[str | None, str]]:
    """The API-error entries in these transcript lines not already seen.

    Pure, and forgiving of everything: a line that is not JSON, an entry that is
    not an error, a shape that has changed so the flag is gone — all produce
    nothing rather than an exception. A false negative is a missed alert; a
    raise would be the feature taking something down with it, which is the one
    outcome ruled out.
    """
    found: list[tuple[str | None, str]] = []
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
        if isinstance(uuid, str) and uuid in seen:
            continue
        found.append((uuid if isinstance(uuid, str) else None, _text_of(entry)))
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


@dataclass
class _Watched:
    transcript: Path
    agent_id: str
    role: Role | None
    session_name: str | None
    #: Where the last scan stopped, so each byte is read once.
    offset: int
    last_noted: datetime
    seen: set[str] = field(default_factory=set)


class TranscriptWatcher:
    """Watches active sessions' transcripts for errors no hook reports.

    Given a channel to speak through and a gate to respect, it holds a small map
    of session to watch state and, on a timer, relays any new API error to the
    seat that session belongs to. Nothing here is on the approval path.
    """

    def __init__(
        self,
        *,
        channel,
        gate: Gate | None = None,
        clock=lambda: datetime.now(UTC),
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        idle_ttl: timedelta = DEFAULT_IDLE_TTL,
        roots: tuple[Path, ...] | None = None,
    ) -> None:
        self._channel = channel
        self._gate = gate or Gate()
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._idle_ttl = idle_ttl
        self._watched: dict[str, _Watched] = {}
        # Which directories a transcript may live in. A parameter so a test can
        # point it somewhere real, not so an operator can widen it.
        self._roots = roots

    def note(
        self,
        *,
        session_id: str | None,
        transcript_path: str | None,
        agent_id: str | None,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> None:
        """Record that a session is active, so its transcript is watched.

        Best-effort and total: any bad input is ignored rather than raised on,
        because this is called from inside the approval endpoint and must not be
        able to affect it. Only Claude Code, the one runtime whose transcript is
        read in the shape below.
        """
        try:
            if agent_id != CLAUDE_CODE or not session_id:
                return
            safe = inside_a_transcript_root(transcript_path, self._roots)
            if safe is None:
                return
            now = self._clock()
            existing = self._watched.get(session_id)
            if existing is not None:
                # Keep the offset — the point is to read only what is appended
                # after we started watching — but refresh the rest.
                existing.transcript = safe
                existing.role = role
                existing.session_name = session_name
                existing.last_noted = now
                return
            # New session: start from the end of the file, so history is not
            # replayed and the first read is not a scan of the whole transcript.
            offset = self._size(safe)
            self._watched[session_id] = _Watched(
                transcript=safe,
                agent_id=agent_id,
                role=role,
                session_name=session_name,
                offset=offset,
                last_noted=now,
            )
        except Exception:
            logger.debug("Could not note a session for transcript watching", exc_info=True)

    async def poll_once(self) -> None:
        """Scan every watched transcript once, and drop the idle ones."""
        cutoff = self._clock() - self._idle_ttl
        for session_id, watched in list(self._watched.items()):
            if watched.last_noted < cutoff:
                del self._watched[session_id]
                continue
            try:
                await self._scan(session_id, watched)
            except Exception:
                # One unreadable transcript must not stop the others being read,
                # and nothing here is worth interrupting anything over.
                logger.debug("Transcript scan failed for %s", session_id, exc_info=True)

    async def run(self) -> None:
        """Poll forever. Cancelled on shutdown, like the channel's own loop."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Transcript poll failed", exc_info=True)

    async def _scan(self, session_id: str, watched: _Watched) -> None:
        lines = self._read_new_lines(watched)
        if not lines:
            return
        for uuid, text in find_api_errors(lines, watched.seen):
            if uuid is not None:
                watched.seen.add(uuid)
            await self._relay(session_id, watched, text)
        # Bound the memory a long-lived session's seen-set can take.
        if len(watched.seen) > MAX_SEEN:
            watched.seen = set(list(watched.seen)[-MAX_SEEN:])

    def _read_new_lines(self, watched: _Watched) -> list[str]:
        """The complete lines appended since the last scan, and no more.

        A partial final line — a write caught mid-flight — is left unread by
        holding the offset before it, so the next poll sees it whole. On a file
        that shrank (replaced or truncated) the offset is reset to the end
        rather than the whole thing re-read, which keeps this cheap on the rare
        occasion a transcript is rewritten.
        """
        size = self._size(watched.transcript)
        if size is None:
            return []
        if size < watched.offset:
            watched.offset = size
            return []
        if size == watched.offset:
            return []
        try:
            with watched.transcript.open("rb") as handle:
                handle.seek(watched.offset)
                raw = handle.read(MAX_READ_BYTES)
        except OSError:
            return []
        last_newline = raw.rfind(b"\n")
        if last_newline == -1:
            return []
        consumed = raw[: last_newline + 1]
        watched.offset += len(consumed)
        return consumed.decode("utf-8", "replace").split("\n")

    async def _relay(self, session_id: str, watched: _Watched, text: str) -> None:
        if self._gate.paused:
            # Paused means the phone is off — the same reason the reply relay
            # stays quiet then. An alert is still a buzz nobody asked for.
            return
        where = watched.session_name or "A session"
        try:
            await self._channel.send_message(
                # Routes to wherever this session's replies already go.
                session_id,
                f"⚠️ <b>{where}</b> stopped on a server error:\n\n{text}",
                watched.role,
                agent_id=watched.agent_id,
                session_name=watched.session_name,
            )
        except Exception:
            logger.debug("Could not relay a transcript error", exc_info=True)

    @staticmethod
    def _size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None
