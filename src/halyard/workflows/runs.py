"""Where a workflow has got to, kept so a restart does not lose it.

A run is one flow going through its steps for one piece of work: which
workflow, which step, which seat it is waiting for, and since when. The machine
restarts often — a merge, a `make restart` — and a flow that forgot itself
there would leave a seat holding a reply nobody was waiting for.

Kept the way the round counter is (`halyard.handoffs.rounds`): a small file
beside the database, written whole each time, and never raising on the path
that is delivering something. One run per piece of work, because a project has
one working tree and its branch says which work that is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many pieces of work to remember, as in `rounds`.
WORKS = 200


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

    def held(self, why: str) -> Run:
        """The same run, stopped for this reason.

        The seat it last dealt with is kept: what it carries on with, if
        somebody sends the step it stopped before, is that seat's reply.
        """
        return replace(self, waiting=False, stopped=why)

    def waiting_on(self, label: str) -> Run:
        """The same run, its step delivered and that seat's reply awaited."""
        return replace(self, waiting=True, waiting_for=label, stopped="", back=False)

    def at(self, step: int, *, back: bool = False, carried: str = "") -> Run:
        """The same run, moved to a step that has not been delivered yet.

        The seat it last heard from stays: that reply is what the step carries,
        whether it goes now or after a stop somebody sends it on from.
        """
        return replace(self, step=step, back=back, carried=carried, waiting=False, stopped="")


def _load(where: Path) -> dict:
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


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
    )


def save(where: Path, work: str, run: Run) -> None:
    """Write where the run has got to. Best-effort, as `rounds.record` is."""
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
    }
    if len(noted) > WORKS:
        noted = dict(
            sorted(noted.items(), key=lambda item: str(item[1].get("since") or ""), reverse=True)[
                :WORKS
            ]
        )
    _write(where, noted)


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
