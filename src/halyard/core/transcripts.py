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

#: How often the configured seats' own sessions are looked up again. Each one
#: is a directory walk, so not every poll; often enough that a session started
#: after the control plane is watched within a few minutes of somebody opening
#: it.
ADOPT_EVERY = timedelta(minutes=5)

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
                # The quietest of the failures: nothing is watched, nothing is
                # wrong anywhere else, and the first sign is a limit filling up
                # with no warning. Said once, when the session is first seen.
                logger.info(
                    "No transcript for %s under %s, so it cannot be watched",
                    session_id,
                    watching.home,
                )
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
                # Said out loud, because "nothing was sent" and "nothing was
                # watched" look identical from a phone. When a usage limit
                # filled up unannounced there was no way to tell which had
                # happened without reading this module.
                logger.info(
                    "No longer watching %s: nothing appended to %s for %s",
                    session_id,
                    watched.transcript.name,
                    self._idle_ttl,
                )
                del self._watched[session_id]
                continue
            try:
                await self._scan(session_id, watched)
            except Exception:
                # One unreadable transcript must not stop the others being read,
                # and nothing here is worth interrupting anything over.
                logger.debug("Transcript scan failed for %s", session_id, exc_info=True)

    def adopt(self, seats) -> None:
        """Watch the sessions the configuration names, without being asked to.

        Everything else here learns about a session because that session called
        in — an approval, or a reply being relayed. That was the whole of it,
        and it left the longest-running sessions least watched: one resumed
        since July ran past both its usage limits with no warning sent, because
        it had said nothing to Halyard since the last restart and `_watched` is
        held in memory. The readings were in its transcript the whole time,
        reaching a hundred per cent on both windows.

        A seat's `session:` is the configuration saying "this one is mine".
        Resolving it costs a directory walk per seat, so this is called at
        startup and on a slow timer rather than on every poll.

        Best-effort throughout. A seat naming a session that does not exist yet
        is the ordinary case on a machine where nobody has started it, and it
        resolves on a later pass.
        """
        from halyard.agents import registry

        for seat in seats or ():
            if not getattr(seat, "session", None):
                continue
            try:
                spec = registry.get(seat.runtime)
                if spec is None or spec.watching is None:
                    continue
                if (found := spec.find_session(seat.session)) is None:
                    continue
                # Resolved every pass rather than once, because a name is not a
                # session: somebody starting a new one under the same name is
                # how a seat comes to point somewhere else. `note` keeps the
                # offset for a session already being read, so this costs a
                # directory walk and never replays history — and it refreshes
                # the clock, so a configured seat is not dropped for being
                # quiet. It is named in the configuration; that is enough.
                self.note(
                    session_id=found.session_id,
                    agent_id=seat.runtime,
                    role=seat.role,
                    session_name=seat.session,
                )
            except Exception:
                logger.debug("Could not adopt the session for seat %s", seat.label, exc_info=True)

    async def run(self, seats=None) -> None:
        """Poll forever. Cancelled on shutdown, like the channel's own loop."""
        self.adopt(seats)
        since_adopting = 0.0
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.poll_once()
                since_adopting += self._poll_seconds
                if since_adopting >= ADOPT_EVERY.total_seconds():
                    since_adopting = 0.0
                    # Again, because a seat's session can be started long after
                    # the control plane was, and because a resumed one may have
                    # a different id than it had at startup.
                    self.adopt(seats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Transcript poll failed", exc_info=True)

    async def _scan(self, session_id: str, watched: _Watched) -> None:
        lines = self._read_new_lines(watched)
        if not lines:
            return
        # A transcript being appended to is a session that is working, and that
        # is what "still active" has to mean here.
        #
        # It used to mean "asked Halyard something in the last half hour",
        # because `last_noted` was only ever set by the approval and message
        # endpoints. A session whose runtime lets most calls through without a
        # card says nothing to either for long stretches, so it aged out while
        # running — and then the thing this watcher exists for happened to it
        # unwatched. Measured on a second machine: a Codex session ran past its
        # usage limit and stopped, and no warning was sent, because half an
        # hour of quiet work had already dropped it.
        #
        # Which was self-defeating in the exact way that is easy to miss. This
        # watcher is here for the turns that report nothing; keying its own
        # attention to a session reporting something meant the sessions it was
        # written for were the ones it stopped looking at.
        watched.last_noted = self._clock()
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
            #
            # Logged, because from a phone this is indistinguishable from the
            # alert never having been found, and somebody who paused an hour
            # ago has usually stopped thinking about it.
            logger.info("Paused, so not relaying for %s: %s", session_id, text)
            return
        # Every other outcome on this path used to be silent — sent, suppressed,
        # never watched, never found — and when a usage limit filled up with no
        # warning there was no way to tell which of them had happened. An alert
        # is rare by construction, so saying so costs nothing.
        logger.info("Relaying for %s: %s", session_id, text)
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


#: How much of a transcript's end to read when somebody asks where they stand.
#:
#: The reading is written on every turn, so the newest one is near the end and
#: this never needs the whole file — which for a long session is megabytes, read
#: while somebody waits for a status screen.
USAGE_TAIL = 256_000


def usage_for(
    session_id: str | None, watching, roots: tuple[Path, ...] | None = None
) -> tuple[str, ...]:
    """How full this session's usage windows are, in the runtime's own words.

    Empty for a runtime that does not say — Claude Code is one, measured on
    2.1.246: no usage command on the CLI, and a transcript carrying per-turn
    token counts with no limit anywhere in them. Codex writes its accounting
    into every turn, so it can answer.

    Never raises. This is a line on a status screen; a session whose file has
    been rotated away should cost that line and nothing else.
    """
    if watching is None or getattr(watching, "usage", None) is None:
        return ()
    found = find_transcript(session_id, watching, roots)
    if found is None:
        return ()
    try:
        with found.open("rb") as reading:
            reading.seek(0, 2)
            reading.seek(max(0, reading.tell() - USAGE_TAIL))
            # The first line is very likely cut in half by the seek; the parser
            # skips what it cannot read, so it costs nothing to hand it over.
            lines = reading.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    try:
        return tuple(watching.usage(lines))
    except Exception:
        logger.warning("Could not read usage from %s", found, exc_info=True)
        return ()
