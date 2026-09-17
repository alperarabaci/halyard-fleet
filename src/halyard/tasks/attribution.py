"""Which seats worked on a task, written onto the task itself.

A task on the tracker is where the history of a piece of work is kept, and it
had no record of *who* did it — which runtime, in which role. That was added by
hand, one label at a time, and a hand-kept record is the one that lapses on a
busy day. Here the seat is already known: every hook says which session it came
from, and the configuration says which runtime and role that session is. So the
first time a seat is heard from on a branch named for a task, the task gets that
seat's label — `navigator:claude`, `reviewer:codex`. The role comes first: it is
what a task's history is read for, and which runtime held it is the detail.

**One label per seat.** By default, role and runtime and nothing else. Not the
session name — the same seat is `alpha-engine-navigator` on one machine and
`macmini-navigator` on another, and the record would split one seat in two. Not
the model — a seat keeps its runtime and its role, but its model is chosen at the
desk and changes under it. Measured in one project's transcripts: five sessions
of eighty-seven changed model partway through, and back again.

**The seat is looked up, not guessed.** A session is matched to its seat the way
a card is matched to its chat: by the name its runtime knows it by, or its id,
always together with the runtime — or, for a runtime whose sessions have no
name to go by, by the codebase, when only one seat of that runtime works in it.
A role the session declared itself, with `HALYARD_ROLE`, stands in only where
no seat matches.

**Roles are optional.** A seat given none is labelled with its runtime alone —
`claude` — which is the whole story in a project with one seat. Where a project
does give this runtime's seats roles, a session no seat matches and that
declared none could be any of them, or none, and it is not labelled at all
rather than guessed at: a guess would put a second label on one seat. That is
said in the log, once per session.

**A seat can say otherwise**, with `task_label:` — to match labels a tracker
already uses. It is written exactly as given, so the same rule holds: a model
put in it is right until the seat's model changes, and nothing here would
notice. Matched on runtime and role, because that is all a sighting carries; two
seats in one project that share both and ask for different labels are refused
when the configuration loads, since nothing here could tell them apart.

**One colon.** GitLab reserves `::` for scoped labels on its paid tiers, where two
labels in one scope replace each other — `driver::codex` would take
`driver::claude` off a task both drivers worked on. A default that behaved differently
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
from halyard.core.seats import for_project, for_session
from halyard.tasks.branches import current as current_branch
from halyard.tasks.branches import number_of
from halyard.tasks.registry import build
from halyard.tasks.remotes import origin_of
from halyard.tasks.spec import ForgeError

logger = logging.getLogger(__name__)


def label_for(tag: str, role: Role | None) -> str:
    """What a seat puts on a task unless it names its own: `navigator:claude`,
    or `claude` alone for a seat given no role."""
    return f"{role.value}:{tag}" if role is not None else tag


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
        #: Sessions already said to match no seat, so each is said once.
        self._said_no_seat: set[str] = set()
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
        label = self._label_for(name, settings, session)
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

    def _label_for(self, project: str, settings: object, session: SessionInfo) -> str | None:
        """The label of the seat this session is: the seat's own `task_label:`
        if it named one, else its role and runtime.

        The seat is found the way a card finds its chat: by the name the runtime
        knows the session by, or its id, always together with the runtime; then,
        for a runtime whose sessions have no name to go by, by the codebase,
        which answers nothing when two seats of one runtime share it. See
        `seats.for_session`. Before this looked, a session started from the
        desktop app carried no role — only one launched with `HALYARD_ROLE` does
        — and in a project whose seats all have roles no task was labelled.

        Found nowhere, a role the session declared itself stands in, and a seat
        given no role is labelled with its runtime alone. Where the project gives
        roles to this runtime's seats, a session without one could be any of
        them, or none — so it is left unlabelled, and that is said once.
        """
        seats = list(getattr(settings, "seats", None) or ())
        runtime = session.agent_id
        seat = for_session(seats, runtime, session.session_name, session.session_id)
        seat = seat or for_project(seats, runtime, project)
        if seat is not None:
            role, chosen = seat.role, seat.task_label
        else:
            role = session.role
            chosen = next(
                (
                    s.task_label
                    for s in seats
                    if s.runtime == runtime and s.role == role and s.task_label
                ),
                None,
            )
            roles = any(s.runtime == runtime and s.role is not None for s in seats)
            if role is None and not chosen and roles:
                self._no_seat(project, session)
                return None
        if chosen:
            return chosen
        tag = self._tag_of(runtime)
        return label_for(tag, role) if tag else None

    def _no_seat(self, project: str, session: SessionInfo) -> None:
        """Say, once per session, that it could not be tied to a seat.

        Nothing said it before, and labelling that never happened looked the
        same in the log as labelling nobody needed.
        """
        if session.session_id in self._said_no_seat:
            return
        self._said_no_seat.add(session.session_id)
        logger.info(
            "Not labelling for %s session %s in %s: it matches no seat",
            session.agent_id,
            session.session_name or session.session_id,
            project,
        )

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
