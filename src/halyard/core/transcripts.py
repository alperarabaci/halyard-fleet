"""Notice what a runtime never says out loud, and put it on the phone.

Some things fire no hook at all. A turn that dies on an API error does not
reach `Stop`, because from the runtime's side it did not *finish responding*,
it broke — so the reply relay never runs and a session simply goes quiet. A
usage window filling up is not an event anywhere. Both are visible only in the
file the runtime writes as it goes, so this polls that file.

**What is in the file is the runtime's business, not this module's.** Where the
transcripts live, how one is named, and what in it is worth a message all come
from `RuntimeSpec.watching`. That was learned the expensive way: the first
version of this file had `CLAUDE_CODE = "claude-code"` and a Claude-shaped
parser in it, and worked perfectly until Codex needed the same thing with a
different filename, a different entry shape, and a different thing worth saying
— a percentage climbing rather than wreckage after the fact.
`tests/test_runtime_isolation.py` now fails if a runtime is named here again.

Two rules shape every line here, both asked for directly:

**It must never break the gate.** This is a courier for a nice-to-have alert,
not part of the approval path, so every file read, every parse, every push is
wrapped and any failure is swallowed. A transcript that has moved, a format that
has changed, a permission that was revoked — each makes this quietly do nothing,
never raise.

**It must stay cheap.** Only sessions a hook has actually mentioned are watched,
the poll reads only the bytes appended since last time, an idle session is
dropped, and a transcript is located once rather than on every approval.
"""

from __future__ import annotations

import asyncio
import logging
import re
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

#: What a session id may look like before it is allowed to name a file. Hex and
#: dashes: every runtime's id measured so far is a UUID. No dot and no separator
#: can pass, so nothing here can climb out of a directory or name a file of its
#: own choosing — which is why the id is used instead of the path a payload
#: offered. This stays in core: it is a security boundary, not a runtime's taste.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def watching_for(agent_id: str | None):
    """How to watch this runtime, or None if it is not watched.

    Asked of the registry rather than decided here. Core knows that transcripts
    are polled, that bytes are read once, and that a thing is not said twice; it
    knows nothing about which runtime writes what, and a `if agent_id ==` in
    this file is the shape that made adding Codex a rewrite rather than a
    package. `tests/test_runtime_isolation.py` keeps it that way.
    """
    if not agent_id:
        return None
    try:
        from halyard.agents import registry

        spec = registry.discover().get(agent_id)
    except Exception:
        return None
    return spec.watching if spec is not None else None


def find_transcript(session_id: str | None, watching, roots: tuple[Path, ...] | None = None):
    """This session's transcript, found by the runtime and checked by core.

    The runtime says how one of its files is named; core says where it may be.
    Both halves matter: the finder is the only thing that knows the shape, and
    the containment check is the only thing standing between an id posted over
    HTTP and any file on the machine.
    """
    if not session_id or watching is None or not _SESSION_ID.match(session_id):
        return None
    root = Path(roots[0]) if roots else Path(watching.home)
    try:
        found = watching.transcript(session_id, root)
        if found is None:
            return None
        resolved = Path(found).resolve()
        resolved.relative_to(root.expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


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
        agent_id: str | None,
        role: Role | None = None,
        session_name: str | None = None,
    ) -> None:
        """Record that a session is active, so its transcript is watched.

        Best-effort and total: any bad input is ignored rather than raised on,
        because this is called from inside the approval endpoint and must not be
        able to affect it. A runtime with no `watching` is left alone, which is
        the honest answer for one whose file shape nobody has measured.
        """
        try:
            watching = watching_for(agent_id)
            if watching is None or not session_id:
                return
            now = self._clock()
            existing = self._watched.get(session_id)
            if existing is not None:
                # Already found once. Looking it up again on every approval
                # would be a directory walk per gated command.
                # Keep the offset — the point is to read only what is appended
                # after we started watching — but refresh the rest.
                existing.role = role
                existing.session_name = session_name
                existing.last_noted = now
                return
            # New session: start from the end of the file, so history is not
            # replayed and the first read is not a scan of the whole transcript.
            found = find_transcript(session_id, watching, self._roots)
            if found is None:
                return
            offset = self._size(found)
            self._watched[session_id] = _Watched(
                transcript=found,
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
        watching = watching_for(watched.agent_id)
        if watching is None:
            return
        for alert in watching.alerts(lines, watched.seen):
            watched.seen.add(alert.key)
            await self._relay(session_id, watched, alert.text)
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
                f"⚠️ <b>{where}</b> {text}",
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
