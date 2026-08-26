"""What a session is told after half its context has been thrown away.

Compaction is where a long agentic session quietly gets worse. The summary
keeps the shape of the work and loses the things that were expensive to
establish: which numbers were measured rather than claimed, what the operator
already rejected and why, which corrections were made to the agent's own
earlier mistakes. What comes back is a confident agent working from a version
of events that is subtly older than the one it had.

A hook cannot fix the summary. Measured, on a live session: `PreCompact` fires
before the compaction and its `additionalContext` never reaches the model —
the session was asked afterwards for a value injected there and did not have
it, and said it had disregarded the attempt. That is the right call by the
runtime, and it is the end of that road.

What does work is the other side of it. `SessionStart` fires again once the
compaction is done, with `source: "compact"`, and *its* `additionalContext`
does reach the model — the same measurement, with the token that was injected
there, came back verbatim. So this is the half that exists: a file per seat,
handed over the moment the context is fresh, saying what to read and what the
standing rules are.

**Per seat, because the seats differ.** A navigator holding a plan across a
day needs its orientation back; a driver running one command does not, and
would only be handed a page it has no use for.

Nothing here is on the approval path, and every failure is silent. A file that
cannot be read means no context is injected, which leaves the session exactly
as compaction left it — worse than being oriented, and much better than a
session that will not start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from halyard.core.seats import Seat, for_session

logger = logging.getLogger(__name__)

#: Enough for a page of orientation, and a bound on what one bad file can push
#: into a session's context. The measured example runs to about 1,600 bytes.
LIMIT = 32_000

#: The `source` a `SessionStart` payload carries when the session has just been
#: compacted, as opposed to opened. Measured; the other value seen is "startup".
AFTER_COMPACTION = "compact"


def read(path: str | Path, root: Path | None = None) -> str | None:
    """The text to inject, or None if there is nothing to inject.

    Relative paths are resolved against the Halyard checkout rather than the
    working directory, because the hook that asks for this runs inside somebody
    else's process tree and its working directory is a fact about that session,
    not about where the file was written.
    """
    try:
        wanted = Path(path).expanduser()
        if not wanted.is_absolute():
            wanted = (root or Path.cwd()) / wanted
        text = wanted.read_text(encoding="utf-8").strip()
    except OSError as error:
        # Said once, at the level of something worth fixing: a seat configured
        # with a file that is not there gets nothing, silently, for as long as
        # nobody looks. `doctor` reports it too.
        logger.warning("Could not read the post-compaction file %s: %s", path, error)
        return None
    if not text:
        return None
    if len(text) > LIMIT:
        logger.warning("Post-compaction file %s is over %d bytes; truncating", path, LIMIT)
        text = text[:LIMIT]
    return text


def for_seat(
    seats: list[Seat],
    *,
    agent_id: str | None,
    session_name: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> str | None:
    """What this session should be told, if its seat says anything at all.

    The seat is resolved by `(runtime, session)` like everything else that
    routes — a Claude driver and a Codex driver can hold the same name, and
    handing one the other's orientation would be worse than handing it none.
    """
    seat = for_session(seats, agent_id, session_name, session_id)
    if seat is None or not seat.after_compaction:
        return None
    return read(seat.after_compaction, root)


#: How much of a transcript's tail the one-shot turn reads. This never enters
#: the live session: it is the *input* to a separate turn, in a session of its
#: own, and only that turn's answer is carried across.
TAIL_BYTES = 200_000

#: How much of that answer is allowed back. The point of a compaction is that
#: the context was full; carrying a long record across would refill what was
#: just emptied and buy a second compaction an hour later. This is the size of
#: a page — the measured orientation file is about 1,600 characters — and it is
#: asked for in the prompt as well as enforced here, because a model told to be
#: brief writes something better than a model whose answer is cut off.
RECORD_LIMIT = 4_000

#: Which model writes the record. It is a distillation of text somebody else
#: already wrote — the reasoning was done in the session, not here — so this
#: does not need the expensive one. Override with HALYARD_COMPACTION_MODEL.
RECORD_MODEL = "sonnet"

#: How long the record may take before the compaction goes ahead without it.
#: `PreCompact` blocks while this runs, which is the trade taken deliberately:
#: a compaction that starts a minute later is better than a record that arrives
#: after the context it was about is gone. Past this the summary proceeds.
RECORD_TIMEOUT_SECONDS = 120.0


def conversation_tail(path: str | Path, limit: int = TAIL_BYTES) -> str:
    """The recent conversation as plain text, for a model to read.

    Read from the end, because the end is what a compaction is about to lose
    the detail of. Forgiving of everything: a line that will not parse, an
    entry with no text, a file that has moved. The worst outcome here is a
    thinner record, never a raised exception — nothing in this file is allowed
    to interrupt a session.
    """
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            raw = handle.read()
    except OSError:
        return ""

    lines = raw.decode("utf-8", "replace").split("\n")
    said: list[str] = []
    for line in lines[1:] if len(lines) > 1 else lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("isApiErrorMessage"):
            continue
        who = entry.get("type")
        if who not in ("user", "assistant"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
        else:
            continue
        text = " ".join(part.strip() for part in parts if part.strip())
        if text:
            said.append(f"{who}: {text}")
    return "\n\n".join(said)


class Recorder:
    """Writes the record a compaction is about to make unrecoverable.

    The shape of this is forced by what was measured. A hook cannot make a model
    write anything, and `PreCompact` output is refused by the runtime outright —
    so the record cannot be produced *by* the session. It is produced *about*
    it, by a separate one-shot turn that reads the transcript while `PreCompact`
    holds the compaction, and is handed back when `SessionStart` returns.

    That separation is not incidental: resuming the session to ask it for a
    summary would fork the conversation, which this project has measured and
    written down. A one-shot turn shares nothing with it — and because it is a
    different session, the transcript it reads never enters the live context.
    Only the answer crosses, and only up to `RECORD_LIMIT` of it.

    Everything is best effort. A record that is late, not configured, or that
    could not be written leaves the session with whatever `after_compaction`
    says and nothing else.
    """

    def __init__(
        self,
        *,
        seats: list[Seat],
        runners: dict,
        root: Path | None = None,
        model: str | None = RECORD_MODEL,
        clock=time.monotonic,
    ) -> None:
        self._seats = list(seats)
        self._runners = dict(runners or {})
        self._root = root or Path.cwd()
        self._model = model or RECORD_MODEL
        self._clock = clock
        # The record, and when it was written. The second half is only there to
        # measure with: `PreCompact` to `SessionStart` is the compaction itself,
        # and that number is the one worth having when asking whether holding
        # the summary for a record was worth it.
        self._records: dict[str, tuple[str, float]] = {}

    async def write(
        self,
        *,
        session_id: str,
        agent_id: str | None,
        session_name: str | None,
        transcript_path: str | None,
    ) -> bool:
        """Write this session's record now. Returns whether one was produced.

        Awaited by the `PreCompact` endpoint, so the compaction waits for it.
        Never raises: the caller is a hook holding up a session, and every
        failure here has to end in the compaction simply going ahead.
        """
        seat = for_session(self._seats, agent_id, session_name, session_id)
        if seat is None or not seat.before_compaction or not transcript_path:
            return False
        runner = self._runners.get(seat.runtime)
        if runner is None or not hasattr(runner, "ask"):
            # Only Claude Code can run a one-shot turn today. A seat on another
            # runtime is left alone rather than reached for with a method it
            # does not have.
            return False
        instructions = read(seat.before_compaction, self._root)
        if instructions is None:
            return False

        started = self._clock()
        try:
            conversation = conversation_tail(transcript_path)
            if not conversation:
                return False
            record = await asyncio.wait_for(
                runner.ask(self._prompt(instructions, conversation), model=self._model),
                timeout=RECORD_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "The pre-compaction record for %s ran past %.0fs; compacting without it",
                session_id,
                RECORD_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            logger.warning("Could not write a pre-compaction record", exc_info=True)
            return False

        if not record:
            return False
        if len(record) > RECORD_LIMIT:
            logger.info(
                "Record for %s was %d chars; trimmed to %d", session_id, len(record), RECORD_LIMIT
            )
            record = record[:RECORD_LIMIT].rstrip()
        self._records[session_id] = (record, self._clock())
        # The numbers worth having when asking whether this is worth its cost:
        # what it took, and how much came back.
        logger.info(
            "Pre-compaction record for %s: %d chars in %.1fs on %s",
            session_name or session_id,
            len(record),
            self._clock() - started,
            self._model,
        )
        return True

    def _prompt(self, instructions: str, conversation: str) -> str:
        return (
            f"{instructions}\n\n"
            "---\n\n"
            f"Keep the whole record under {RECORD_LIMIT} characters. It is about to be "
            "put into a context that was just emptied on purpose, and a long one would "
            "refill it. Facts and numbers, no prose, no preamble, no offer to help. "
            "Answer in the language the conversation uses.\n\n"
            "Below is the recent conversation of the session this is about.\n\n"
            f"{conversation}"
        )

    def take(self, session_id: str) -> str | None:
        """This session's record, once. A record describes one compaction, and
        leaving it behind would hand it to the next one as though it were fresh.
        """
        found = self._records.pop(session_id, None)
        if found is None:
            return None
        record, written_at = found
        # What the compaction itself cost, which nothing else in this system can
        # see: the gap between the hook that held it and the one that came back.
        logger.info(
            "Compaction of %s took %.1fs; carrying %d chars across",
            session_id,
            self._clock() - written_at,
            len(record),
        )
        return record
