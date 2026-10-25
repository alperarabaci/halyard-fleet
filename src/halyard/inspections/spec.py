"""What an inspection needs from outside, and what it gives back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class Gated(Protocol):
    """An `Asker` whose turns can stop a read to ask a person first.

    A read whose card was denied, expired or still open when the turn ended
    never ran, and a model may answer without it: Codex goes on with other
    reads and answers while a card is still open. Once a turn is over, this
    says which reads did not run, so the answer can carry it in Halyard's own
    words rather than leave it to the model's.
    """

    def not_run(self, session_id: str) -> tuple[NotRun, ...]: ...


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
    #: The reads it asked for that did not run, in the order it asked.
    not_run: tuple[NotRun, ...] = ()

    @property
    def measured(self) -> bool:
        """Whether there is anything to read. An unmeasured inspection is never a
        clean one."""
        return self.text is not None
