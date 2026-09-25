"""Where a workflow has got to, kept so a restart does not lose it.

A run is one flow going through its steps for one piece of work: which
workflow, which step, which phase, which seat it is waiting for, and since
when. The machine restarts often — a merge, a `make restart` — and a flow that
forgot itself there would leave a seat holding a reply nobody was waiting for.

**A run counts its own rounds.** Each step's rounds are kept in the run, by the
step's name — and inside a flow's phases by the step and the phase, so the
second phase's `discover` starts from its first round. A transition pressed by
hand is not part of any run, and counts nothing: the rounds that stop a loop
are the loop's own. A run that is stopped and started again starts counting
again, because starting one is somebody deciding to.

**A round is a message that arrived.** It is counted when the seat's session
takes it, so a step that reached nobody is not a round, and sending it again
is the same round rather than the next.

Kept the way `last_said` is: a small file beside the database, written whole
each time, and never raising on the path that is delivering something. One run
per piece of work, because a project has one working tree and its branch says
which work that is.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many pieces of work to remember.
WORKS = 200


@dataclass(frozen=True)
class Round:
    """One time a step reached the seat it was for."""

    at: datetime
    #: The seat's label. The answer to this round is what that seat says next.
    to: str
    #: What the run did on that answer — `forward`, `back`, `wait`, `next` —
    #: and whose word it was: this step's own, or the one before it when this
    #: step acts on it (`decided_by:`). Empty until the answer comes, and for
    #: an answer that decided nothing.
    decision: str = ""
    decided_by: str = ""


@dataclass(frozen=True)
class Run:
    """One workflow, part-way through its steps."""

    workflow: str
    #: Which step of the flow, counting from zero. The one being waited on
    #: while `waiting`, and the one that has not gone yet while `stopped`.
    step: int
    since: datetime
    #: Where the run was started, and where its lines go.
    chat: str
    thread: int | None = None
    #: The seat the run last dealt with: whose reply it waits for while it is
    #: `waiting`, and whose reply the next step carries when it goes — kept
    #: when the run moves on or stops, so a step sent later carries that reply.
    waiting_for: str = ""
    waiting: bool = True
    #: Whether the step that has not gone yet is a return to an earlier one.
    back: bool = False
    #: Why it is not going on. Empty while it runs.
    stopped: str = ""
    #: Who started it, so the steps it takes by itself are still attributed.
    by: str = ""
    #: What the step before decided, for a step that acts on it — one whose
    #: `decided_by:` names that step. Empty for every other step.
    carried: str = ""
    #: Which phase the run is in. One until a flow's phases go round; the
    #: steps before them are in the first, and the steps after them in the last.
    phase: int = 1
    #: Where this phase started, as a place in the flow — `back` from there
    #: goes to the end of the phase before. -1 until a phase has started.
    entered: int = -1
    #: Every round each step has had in this run, oldest first, by what it is
    #: counted as — see `counted_as`.
    rounds: Mapping[str, tuple[Round, ...]] = field(default_factory=dict)
    #: For a run stopped at the end of a phase, with the next one made ready:
    #: the place leaving the phases would take it instead. -1 for any other
    #: stop, and gone as soon as the run moves — a button left on an old card
    #: then finds nothing to leave.
    leaving: int = -1

    def held(self, why: str, *, leaving: int = -1) -> Run:
        """The same run, stopped for this reason.

        The seat it last dealt with is kept: what it carries on with, if
        somebody sends the step it stopped before, is that seat's reply.
        """
        return replace(self, waiting=False, stopped=why, leaving=leaving)

    def waiting_on(self, label: str) -> Run:
        """The same run, its step delivered and that seat's reply awaited."""
        return replace(self, waiting=True, waiting_for=label, stopped="", back=False, leaving=-1)

    def at(
        self,
        step: int,
        *,
        back: bool = False,
        carried: str = "",
        phase: int | None = None,
        entered: int | None = None,
    ) -> Run:
        """The same run, moved to a step that has not been delivered yet.

        The seat it last heard from stays: that reply is what the step carries,
        whether it goes now or after a stop somebody sends it on from.
        """
        return replace(
            self,
            step=step,
            back=back,
            carried=carried,
            waiting=False,
            stopped="",
            phase=self.phase if phase is None else phase,
            entered=self.entered if entered is None else entered,
            leaving=-1,
        )

    def taken(self) -> dict[str, int]:
        """How many rounds each step has had in this run, by what it is counted as."""
        return {key: len(rounds) for key, rounds in self.rounds.items()}


def counted_as(name: str, place: int, *, phase: int, stretch: tuple[int, int] | None) -> str:
    """What a step's rounds are counted under: its name, and inside a flow's
    phases its phase as well — `discover@2`."""
    if stretch is not None and stretch[0] <= place <= stretch[1]:
        return f"{name}@{phase}"
    return name


def _load(where: Path) -> dict:
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _rounds_from(value: object) -> dict[str, tuple[Round, ...]]:
    found: dict[str, tuple[Round, ...]] = {}
    for key, entries in value.items() if isinstance(value, dict) else ():
        kept: list[Round] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                at = datetime.fromisoformat(str(entry.get("at")))
            except (TypeError, ValueError):
                continue
            kept.append(
                Round(
                    at=at if at.tzinfo else at.replace(tzinfo=UTC),
                    to=str(entry.get("to") or ""),
                    decision=str(entry.get("decision") or ""),
                    decided_by=str(entry.get("decided_by") or ""),
                )
            )
        found[str(key)] = tuple(kept)
    return found


def current(where: Path, work: str) -> Run | None:
    """The run this piece of work has, if it has one."""
    entry = _load(where).get(work)
    if not isinstance(entry, dict):
        return None
    try:
        since = datetime.fromisoformat(str(entry.get("since")))
    except (TypeError, ValueError):
        return None
    workflow = str(entry.get("workflow") or "")
    if not workflow:
        return None
    thread = entry.get("thread")
    phase = entry.get("phase")
    entered = entry.get("entered")
    leaving = entry.get("leaving")
    return Run(
        workflow=workflow,
        step=int(entry.get("step") or 0),
        since=since if since.tzinfo else since.replace(tzinfo=UTC),
        chat=str(entry.get("chat") or ""),
        thread=int(thread) if isinstance(thread, int) else None,
        waiting_for=str(entry.get("waiting_for") or ""),
        waiting=bool(entry.get("waiting")),
        back=bool(entry.get("back")),
        stopped=str(entry.get("stopped") or ""),
        by=str(entry.get("by") or ""),
        carried=str(entry.get("carried") or ""),
        phase=phase if isinstance(phase, int) and phase >= 1 else 1,
        entered=entered if isinstance(entered, int) else -1,
        rounds=_rounds_from(entry.get("rounds")),
        leaving=leaving if isinstance(leaving, int) else -1,
    )


def save(where: Path, work: str, run: Run) -> None:
    """Write where the run has got to. Best-effort, as `last_said` is."""
    noted = _load(where)
    noted[work] = {
        "workflow": run.workflow,
        "step": run.step,
        "since": run.since.isoformat(),
        "chat": run.chat,
        "thread": run.thread,
        "waiting_for": run.waiting_for,
        "waiting": run.waiting,
        "back": run.back,
        "stopped": run.stopped,
        "by": run.by,
        "carried": run.carried,
        "phase": run.phase,
        "entered": run.entered,
        "leaving": run.leaving,
        "rounds": {
            key: [
                {
                    "at": entry.at.isoformat(),
                    "to": entry.to,
                    "decision": entry.decision,
                    "decided_by": entry.decided_by,
                }
                for entry in entries
            ]
            for key, entries in run.rounds.items()
        },
    }
    if len(noted) > WORKS:
        noted = dict(
            sorted(noted.items(), key=lambda item: str(item[1].get("since") or ""), reverse=True)[
                :WORKS
            ]
        )
    _write(where, noted)


def record(where: Path, work: str, key: str, *, to: str, now: datetime | None = None) -> int:
    """Note that a step reached its seat, and say which round it was.

    Written into the run as it is now, rather than the one the step was sent
    from: the seat can take the message after the run has been saved again.
    Nothing is counted for a run that is gone — somebody stopped it while its
    step was on its way, and there is nothing left to count against.
    """
    run = current(where, work)
    if run is None:
        return 0
    entries = (*run.rounds.get(key, ()), Round(at=now or datetime.now(UTC), to=to))
    save(where, work, replace(run, rounds={**run.rounds, key: entries}))
    return len(entries)


def answered(run: Run, key: str, *, decision: str, decided_by: str) -> Run:
    """The same run, the latest round of `key` marked with what its answer
    decided. Unchanged when that step has had no round: its message reached
    nobody, so there is no answer to mark."""
    entries = run.rounds.get(key, ())
    if not entries:
        return run
    marked = replace(entries[-1], decision=decision, decided_by=decided_by)
    return replace(run, rounds={**run.rounds, key: (*entries[:-1], marked)})


def clear(where: Path, work: str) -> None:
    """Forget this piece of work's run — it finished, or somebody stopped it."""
    noted = _load(where)
    if noted.pop(work, None) is None:
        return
    _write(where, noted)


def _write(where: Path, noted: dict) -> None:
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(noted), encoding="utf-8")
    except OSError as unwritable:
        logger.warning("Could not write %s: %s", where, unwritable)
