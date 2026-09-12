"""Which seats worked on a task, written onto the task itself.

A task on the tracker is where the history of a piece of work is kept, and it
had no record of *who* did it — which runtime, in which role. That was added by
hand, one label at a time, and a hand-kept record is the one that lapses on a
busy day. Here the seat is already known: every hook says which session it came
from, and the configuration says which runtime and role that session is. So the
first time a seat is heard from on a branch named for a task, the task gets that
seat's label — `claude:navigator`, `codex:reviewer`.

**One label per seat.** By default, runtime and role and nothing else. Not the
session name — the same seat is `alpha-engine-navigator` on one machine and
`macmini-navigator` on another, and the record would split one seat in two. Not
the model — it is not reliably known here, and a label that is sometimes right is
worse for counting than one that is always coarse.

**Roles are optional.** A seat given none is labelled with its runtime alone —
`claude` — which is the whole story in a project with one seat. Where a project
does give this runtime's seats roles, a sighting without one could be any of
them, or a session no seat owns, and it is not labelled at all rather than
guessed at: a guess would put a second label on one seat.

**A seat can say otherwise**, with `task_label:`. A model pinned to a seat is
known to whoever pinned it even though it is not known here, and
`fable:navigator` says more than `claude:navigator` when it is true. Matched on
runtime and role, because that is all a sighting carries; two seats in one
project that share both and ask for different labels are refused when the
configuration loads, since nothing here could tell them apart.

**One colon.** GitLab reserves `::` for scoped labels on its paid tiers, where two
labels in one scope replace each other — `claude::driver` would take
`claude::navigator` off a task both worked on. A default that behaved differently
depending on the tracker's plan would be a bug waiting for an upgrade. A
`task_label:` may use `::` anyway: that is a choice about somebody's own tracker,
made by them.

**Written once.** The task's own labels are read first, and a label already on
it is never written again — whatever the tracker would do with a duplicate, this
does not ask it to find out. What was written or found is remembered, so a
session's hundredth turn costs a branch lookup and nothing else.

**Never on the path of an approval.** `seen` returns at once and the rest runs
in a task of its own; every failure is logged and swallowed. A label that could
not be written is a gap in a record. An approval that waited for one would be a
gate held open by an issue tracker.

A failure is not retried until the control plane restarts. The usual cause is a
token that may not label issues, and asking again on every turn would turn one
refusal into a stream of them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from pathlib import Path

from halyard.core.events import Role
from halyard.core.registry import SessionInfo
from halyard.tasks.branches import current as current_branch
from halyard.tasks.branches import number_of
from halyard.tasks.registry import build
from halyard.tasks.remotes import origin_of
from halyard.tasks.spec import ForgeError

logger = logging.getLogger(__name__)


def label_for(tag: str, role: Role | None) -> str:
    """What a seat puts on a task unless it names its own: `claude:navigator`,
    or `claude` alone for a seat given no role."""
    return f"{tag}:{role.value}" if role is not None else tag


class Attribution:
    """Puts each seat's label on the task its branch is for, once."""

    def __init__(
        self,
        *,
        token: str,
        projects: Mapping[str, object],
        tag_of: Callable[[str], str | None],
    ) -> None:
        #: Only the projects that asked for this, each with a `path`, a `forge`
        #: and its `seats` — see `label_work:` in `halyard.yaml`.
        self._projects = dict(projects)
        self._token = token
        #: Runtime → the short name it labels with (`claude`). Handed in by the
        #: app, which knows the runtimes, so this module never imports one.
        self._tag_of = tag_of
        #: (project, task, label) already on the task, or already tried.
        self._done: set[tuple[str, int, str]] = set()
        # Strong references, so a labelling task is not collected half-way.
        self._running: set[asyncio.Task] = set()

    def seen(self, session: SessionInfo) -> None:
        """A session was heard from. Returns at once; see the module docstring."""
        if not session.cwd:
            return
        project = self._project_for(session.cwd)
        if project is None:
            return
        name, settings = project
        label = self._label_for(settings, session.agent_id, session.role)
        if not label:
            return
        try:
            labelling = asyncio.get_running_loop().create_task(
                self._attribute(Path(session.cwd), name, settings, label)
            )
        except RuntimeError:
            return  # no loop: nothing here can run, and nothing is lost by skipping
        self._running.add(labelling)
        labelling.add_done_callback(self._running.discard)

    def _label_for(self, settings: object, runtime: str, role: Role | None) -> str | None:
        """The seat's own `task_label:` if it named one, else runtime and role.

        A seat given no role is labelled with its runtime alone. Where the project
        gives roles to this runtime's seats, a sighting without one could be any
        of them, or a session no seat owns — so it is left unlabelled.
        """
        seats = getattr(settings, "seats", None) or ()
        for seat in seats:
            if seat.runtime == runtime and seat.role == role:
                chosen = getattr(seat, "task_label", None)
                if chosen:
                    return chosen
        if role is None and any(s.runtime == runtime and s.role is not None for s in seats):
            return None
        tag = self._tag_of(runtime)
        return label_for(tag, role) if tag else None

    def _project_for(self, cwd: str) -> tuple[str, object] | None:
        """The opted-in project this directory is inside, if any."""
        try:
            here = Path(cwd).expanduser().resolve()
        except OSError:
            return None
        for name, settings in self._projects.items():
            root = getattr(settings, "path", None)
            if root is None:
                continue
            try:
                if here.is_relative_to(Path(root).expanduser().resolve()):
                    return name, settings
            except OSError:
                continue
        return None

    async def _attribute(self, cwd: Path, project: str, settings: object, label: str) -> None:
        number: int | None = None
        try:
            branch = await asyncio.to_thread(current_branch, cwd)
            number = number_of(branch or "")
            if number is None:
                return
            key = (project, number, label.casefold())
            if key in self._done:
                return
            # Claimed before the network, so two hooks arriving together make
            # one write rather than two.
            self._done.add(key)
            origin = await asyncio.to_thread(origin_of, cwd)
            if origin is None:
                return
            forge = build(origin, self._token, declared=getattr(settings, "forge", None))
            task = await forge.task(number)
            if label.casefold() in {name.casefold() for name in task.labels}:
                return
            await forge.add_label(number, label)
            logger.info("Labelled task #%s in %s with %s", number, project, label)
        except ForgeError as refused:
            logger.warning("Could not label task #%s with %s: %s", number, label, refused)
        except Exception:
            logger.exception("Labelling task #%s with %s failed", number, label)
