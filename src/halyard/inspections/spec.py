"""What an inspection needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class Status(StrEnum):
    """How an inspection's answer stands, from what the turn read — never
    from what the model says it read."""

    #: Every read it asked for ran.
    COMPLETE = "complete"
    #: At least one did not, and the answer was given without it.
    INCOMPLETE = "incomplete"
    #: There is no answer.
    UNMEASURED = "unmeasured"


class Outcome(StrEnum):
    """What became of one read."""

    #: It ran.
    RAN = "ran"
    #: It ran, and its tool said it failed — a file that is not there.
    ERROR = "error"
    #: The gate let it through, and the runtime does not say what came of it.
    ALLOWED = "allowed"
    #: Refused: by a person, a standing rule, or the hook.
    DENIED = "denied"
    #: Its card expired before anybody answered.
    EXPIRED = "expired"
    #: Its card was still open when the inspection ended.
    ENDED = "ended"
    #: It started, and had not come back when the inspection ended.
    UNFINISHED = "unfinished"


#: The outcomes of a read that ran — a command that exited 1 among them.
_RAN = frozenset({Outcome.RAN, Outcome.ERROR, Outcome.ALLOWED})


@dataclass(frozen=True)
class Read:
    """One read an inspection asked for, and what became of it."""

    #: `Read`, `Grep`, `Glob`, `Bash`.
    tool: str
    #: A path, a pattern or a command. Never what came back.
    what: str
    outcome: Outcome
    #: Who decided, an exit code, or why it did not run: "denied by tg:4242",
    #: "exit 1", "pre-authorized (reads)".
    detail: str = ""

    @property
    def ran(self) -> bool:
        return self.outcome in _RAN


@dataclass(frozen=True)
class Decided:
    """What the gate made of one call an inspection's turn asked to make: a
    card and its answer, or a call let through or refused with nobody asked.
    The gate's side of a read, where the runtime's stream has the other."""

    #: The id the call's hook carried — Claude Code's `tool_use` id, which its
    #: stream carries too. Empty when there was none.
    ref: str
    tool: str
    what: str
    #: None when the gate let it through; otherwise why it never ran.
    stopped: Outcome | None
    detail: str = ""


@dataclass(frozen=True)
class NotRun:
    """A read an inspection asked for that never ran: the command, and why not
    — what became of the card that asked a person to allow it."""

    command: str
    #: "denied by …", "expired before anybody answered", "still waiting for
    #: approval when the inspection ended".
    why: str


@runtime_checkable
class Asker(Protocol):
    """A turn apart from any session: text in, an answer out, or None.

    The one port an inspection runs through. The channel answers it today, with
    Claude Code's runner behind it; any runtime that can take a turn of its own
    can, and that is where an inspection stops depending on one model.

    `cwd` is where the turn runs. `name` is what it goes by wherever it is shown
    to a person — a command it asks to run is a card, and the card has to say
    whose. `edits=False` is a turn with no tool that edits a file: it may still
    search and read files and inspect diffs. The inspection prompt limits shell
    use to reads; the runtime sandbox and project's gate remain in force.
    `session_id` is the id the turn runs under, chosen by the inspection, so
    that the record kept of it and the tokens it used can be found together.
    `effort` is how hard the model thinks, in the runtime's own words; None
    leaves it to the runtime.
    """

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        name: str | None = None,
        edits: bool = True,
        session_id: str | None = None,
        effort: str | None = None,
    ) -> str | None: ...


@dataclass(frozen=True)
class Trace:
    """Every read one inspection's turn asked for, each once, with what became
    of it: what the runtime reported, merged with the gate's record."""

    reads: tuple[Read, ...] = ()
    #: Whether the runtime said what the turn read. When it did not —
    #: opencode — only the reads that met the gate are here.
    reported: bool = False

    @property
    def not_run(self) -> tuple[NotRun, ...]:
        """The reads that did not run, and why, in the order they were asked."""
        return tuple(
            NotRun(read.what, read.detail or read.outcome.value)
            for read in self.reads
            if not read.ran
        )


@runtime_checkable
class Gated(Protocol):
    """An `Asker` whose turns can stop a read to ask a person first.

    A read whose card was denied, expired or still open when the turn ended
    never ran, and a model may answer without it: Codex goes on with other
    reads and answers while a card is still open. Once a turn is over, this
    says what it read and what became of each — from the runtime's own report
    and the gate's record, never from the model — so the answer can be marked
    complete or not, and carry the reads that did not run in Halyard's own
    words rather than the model's.
    """

    def trace(self, session_id: str) -> Trace: ...


@dataclass(frozen=True)
class Kept:
    """One inspection run, whole: what the model was given, what it said, and
    how it went — kept so it can be read, compared and run again later.

    Unlike anything else Halyard records, this keeps the text: the input holds
    the reply that was inspected, and an inspection cannot be compared or
    repeated without it.
    """

    #: The id the turn ran under — the same one its tokens are recorded by.
    session: str
    at: datetime
    name: str
    path: Path
    version: str
    #: The transition it ran for, or empty when it was run by hand.
    transition: str
    model: str
    #: Everything the model was given, exactly as it was sent.
    asked: str
    #: The envelope lines inside it, one fact to a line — where the files
    #: stood among them.
    context: tuple[str, ...]
    note: str
    #: What the model said, or None when it said nothing usable.
    answer: str | None
    #: Why there is no answer, when there is none.
    why: str
    #: Left empty since findings stopped labelling tasks; older runs carry
    #: the phrase their answer said.
    finding: str | None
    took: float
    #: How hard the model was asked to think, or None when that was left to the
    #: runtime — whose default is not the same for every model.
    effort: str | None = None
    #: What Halyard said under the answer about reads that did not run — see
    #: `not_run_block` — or empty when every read it asked for ran.
    not_run: str = ""
    #: `Status`'s value, or empty for a run that was not given one — a repeat.
    status: str = ""
    #: What the turn read, or None where nothing could say — a repeat.
    trace: Trace | None = None


@runtime_checkable
class Keeper(Protocol):
    """Where a finished inspection run is kept. The channel answers it — it
    knows the database, the project and the work — and keeping never fails
    the inspection it describes."""

    async def keep(self, kept: Kept) -> None: ...


class StoppedError(Exception):
    """An inspection's turn ended by a person before it answered.

    Raised by whatever runs the turn, and kept as the reason on an unmeasured
    answer: an inspection somebody stopped says so, rather than reading as a model
    that went quiet.
    """


@dataclass(frozen=True)
class Answer:
    """One inspection's answer — or why there is none, which is an answer too."""

    name: str
    #: The inspection's own file, and the commit that last changed it.
    path: Path
    version: str
    #: What the model said, with a wrapping code fence taken off. None when the
    #: inspection did not run or the model did not answer.
    text: str | None = None
    #: Why there is no text, when there is none.
    why: str = ""
    #: How long the model took, in seconds.
    took: float = 0.0
    #: What it read, and what became of each read.
    trace: Trace = field(default_factory=Trace)

    @property
    def measured(self) -> bool:
        """Whether there is anything to read. An unmeasured inspection is never a
        clean one."""
        return self.text is not None

    @property
    def not_run(self) -> tuple[NotRun, ...]:
        """The reads it asked for that did not run, in the order it asked."""
        return self.trace.not_run

    @property
    def status(self) -> Status:
        """Complete when every read it asked for ran, incomplete when one did
        not, unmeasured when there is no answer."""
        if not self.measured:
            return Status.UNMEASURED
        return Status.INCOMPLETE if self.not_run else Status.COMPLETE
