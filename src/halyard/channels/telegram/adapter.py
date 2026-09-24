"""The Telegram channel adapter.

Sends a card, listens for the button, hands the answer to the approval store.

It decides nothing. Whether a nonce is valid, whether a request is still open,
whether it has already been answered — all of that is the store's, and this file
calls into it and reacts to what comes back. The one judgement it does make is
*who is allowed to press the button*, because that is a fact about the channel
rather than about the approval.

Answers arrive by long polling. A webhook would need a public URL, and the whole
posture of this service is that it does not have one.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from halyard import commits, frame
from halyard import handoffs as handing
from halyard import inspections as inspecting
from halyard import tasks as task_tracker
from halyard import workflows as flowing
from halyard.agents.base import AgentRunner
from halyard.applications import catalogue, desktop
from halyard.channels.telegram import cards, commit_card
from halyard.channels.telegram.api import TelegramApi
from halyard.commands import catalogue as commands_offered
from halyard.commands import inputs as command_inputs
from halyard.commands import labels as command_labels
from halyard.commands import running as commands_running
from halyard.core import last_said, transcripts
from halyard.core import prompts as configured_prompts
from halyard.core.approvals import (
    AlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalRequest,
    ApprovalStore,
    Decision,
    InvalidNonceError,
    ResolutionReason,
    UnknownApprovalError,
)
from halyard.core.audit import (
    AuditLog,
    gate_changed,
    invalid_nonce,
    replayed_callback,
    unauthorized_callback,
    user_message,
)
from halyard.core.config_file import Handoff, ModelChoice, Project
from halyard.core.events import Role
from halyard.core.gate import Gate
from halyard.core.questions import (
    AlreadyAnsweredError,
    QuestionExpiredError,
    QuestionRequest,
    QuestionStore,
    UnknownQuestionError,
)
from halyard.core.questions import (
    InvalidNonceError as QuestionInvalidNonceError,
)
from halyard.core.registry import SessionRegistry
from halyard.core.said_by_a_process import the_useful_end
from halyard.core.seats import Seat, find, for_chat, for_project, for_session
from halyard.core.seats import _default_runtime as default_runtime
from halyard.core.transcripts import watching_for
from halyard.handoffs import rounds
from halyard.tasks.branches import current as current_branch

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: How long a poll waits for something to happen before coming back empty.
POLL_TIMEOUT_SECONDS = 30

#: Backoff after a failed poll, so a Telegram outage does not become a tight
#: loop against their API. Doubles per consecutive failure up to the cap.
POLL_RETRY_SECONDS = 3.0

#: Ceiling on the backoff. A long outage should not turn into a long silence
#: after it ends, so recovery is never more than this far away.
POLL_RETRY_MAX_SECONDS = 30.0


#: What this bot answers, in the order a person meets them.
#:
#: One list, because there were two: a `/help` message written by hand, and
#: nothing at all registered with Telegram. A command the client has never
#: heard of does not appear when you type `/`, so every one of these had to be
#: remembered and typed in full — on a phone, which is the only place this is
#: ever used.
#:
#: Telegram's own limits, worth knowing before adding one: the name is
#: lowercase letters, digits and underscores, at most 32 characters, and the
#: description at most 256. Anything else is rejected for the whole list.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "Send a message into this seat's session"),
    ("forward", "Hand this chat's last reply to another seat"),
    ("inspect", "Run one of this project's inspections over this chat's last reply"),
    ("handoff", "Hand this chat's last reply on, the way this project defines it"),
    ("workflow", "Take this project's handoffs in the order it wrote them down"),
    ("commit", "Commit this branch's work, with a message to approve"),
    ("review_and_commit", "The same, with this project's checks and its review round"),
    ("open", "Open an agent on the machine — claude, codex, gemini"),
    ("command", "Run one of this project's own commands"),
    ("label", "Put a label on the task this branch is for"),
    ("status", "What is happening right now"),
    ("doctor", "Check the configuration and say what is wrong with it"),
    ("options", "Models and effort levels this seat accepts"),
    ("model", "Choose what answers, for turns sent from here"),
    ("effort", "Choose how hard it thinks"),
    ("pause", "Step aside — the runtime decides on its own"),
    ("resume", "Take the gate back"),
    ("help", "This list"),
)

#: Answered when typed, and kept off the menu. `/to` is what a handoff already
#: does from the menu, and one fewer button on a phone is worth more than a
#: second way to the same place; whoever types it still gets it.
UNLISTED: tuple[str, ...] = ("to",)


def reserved_names() -> list[str]:
    """Every name a configured prompt cannot take: the menu's, and the ones
    answered only when typed — a prompt called `to` would never run, because
    `/to` answers first."""
    return [name for name, _ in COMMANDS] + list(UNLISTED)


#: How the prompt names the seat it is waiting for.
#:
#: Written into the message so that a reply to it carries the seat back. That
#: was meant to be the whole mechanism — nothing remembered between the button
#: and the sentence somebody types — and it is not enough on its own, because a
#: sentence typed without replying arrives looking like any other and goes to
#: the seat that owns the chat. Measured twice, on a message meant for somebody
#: else. So the seat is held for a few minutes as well, and that fallback is
#: what carries this now that the prompt no longer forces a reply.
#: What the opencode bridge puts in front of a message about a provider refusing
#: on a rate limit. Written there, read here — the two files are in this
#: repository and the coupling is between our own words, not somebody's prose.
#:
#: It exists so this side can tell "the quota is gone" from every other thing an
#: agent might say, without matching on a provider's sentence. Those get
#: rephrased; a marker we write does not.
OUT_OF_QUOTA = "\u26d4\ufe0f"

ASK_FOR_TEXT = "Send what to {seat}?"
_ASKED = re.compile(r"^Send what to (\S+)\?")

#: How long a picked seat waits for the sentence that goes with it.
#:
#: Long enough to type a paragraph, short enough that a tap abandoned before
#: lunch is not still holding the next thing said after it.
HANDOFF_SECONDS = 300

#: How the prompt names the commit it is waiting for a message for.
#:
#: The same shape as `ASK_FOR_TEXT`, and for the same reason: a reply carries
#: the question back, so the sentence somebody types is matched to the proposal
#: it belongs to rather than to whichever was most recent.
ASK_FOR_MESSAGE = "Send the commit message for {handle}?"
_ASKED_MESSAGE = re.compile(r"^Send the commit message for (\S+)\?")

#: The one-shot model that writes the subject line. Named here rather than in
#: `halyard.commits`, which is deliberately ignorant of runtimes: this is a
#: Claude Code alias, and the channel is what holds the runners.
MESSAGE_MODEL = "sonnet"

#: How long to wait for it. A commit message is one short line; anything slower
#: than this has gone wrong, and the phone should hear that rather than hold.
MESSAGE_TIMEOUT_SECONDS = 120.0

#: The one-shot model each of a project's inspections runs on when neither
#: `HALYARD_INSPECTION_MODEL` nor the inspection's own entry names one — named
#: here for the reason `MESSAGE_MODEL` is. One turn per inspection.
INSPECTION_MODEL = "sonnet"

#: How long one inspection may take. It reads a whole report and may run what the
#: report says was run, each command waiting on a tap first — the bound
#: `validate:` has, for the same kind of work.
INSPECTION_TIMEOUT_SECONDS = 600.0

#: How long a reply waits for its project's files to be read. A local
#: repository answers in well under a second, and one that does not costs the
#: record of where the files stood, never the reply.
TREE_TIMEOUT_SECONDS = 5.0

#: How long an inspection or a handoff waits for its task's labels. A tracker
#: that has not answered by then leaves them off the envelope; nothing else
#: waits on it.
LABELS_TIMEOUT_SECONDS = 10.0

#: How long one of a handoff's commands may run. The bound `validate:` has:
#: somebody is holding a phone waiting for the handoff to go, and the long
#: suites are for `/command`, which reports when it is done.
HANDOFF_COMMAND_TIMEOUT_SECONDS = 600.0

#: What each chat last heard, each chat's last answer per inspection, and where each
#: piece of work's workflow has got to, with its rounds. Kept per project — see
#: `_kept`.
SAID_FILE = "last-said.json"
RESULTS_FILE = "check-results.json"
WORKFLOW_FILE = "workflow-runs.json"


def _local(moment: datetime) -> datetime:
    """A stored UTC time as this machine's clock reads it.

    `last_said` keeps UTC, which is right for a file and wrong for a person: a
    reply that arrived at 23:20 was shown as 20:20, and read as three hours old.
    """
    return moment.astimezone()


def _seat_name(seat: Seat | None) -> str:
    """A seat the way a sentence names it: `drv (driver)`, or just `drv`."""
    if seat is None:
        return ""
    return f"{seat.label} ({seat.role.value})" if seat.role else seat.label


def _seat_being_asked_for(text: str) -> str | None:
    """The seat named by one of our own prompts, or None if this is not one."""
    found = _ASKED.match((text or "").strip())
    return found.group(1) if found else None


def _is_our_own_prompt(text: str) -> bool:
    """Whether this text is something Halyard said, not something somebody typed.

    Every one of these is a question with a forced reply, so it sits in the
    chat looking exactly like a message — and a client anchoring a menu to the
    nearest message can hand one back as the thing to forward. It happened:
    an agent was sent "Send what to nav?" and answered that it did not
    understand.

    A slash command is included for the same reason and was already special
    cased at one call site. There is no message worth forwarding that begins
    with one.
    """
    said = (text or "").strip()
    if not said:
        return False
    return (
        said.startswith("/")
        or _seat_being_asked_for(said) is not None
        or _commit_being_asked_for(said) is not None
    )


def _commit_being_asked_for(text: str) -> str | None:
    """The proposal named by one of our own prompts, or None."""
    found = _ASKED_MESSAGE.match((text or "").strip())
    return found.group(1) if found else None


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Lines:
    """What a project's commands run as, once the task's labels are in them."""

    #: Each command's line, filled. Empty when one could not be.
    lines: dict[str, str]
    #: The first label group a command takes a value from and nothing gave one.
    missing: str = ""
    #: The command that takes it.
    needing: str = ""
    #: The task the branch is for, when it names one that could be reached.
    task: int | None = None


@dataclass(frozen=True)
class _Asked:
    """A value a command asked for, answered by the next message in its chat."""

    project: str
    #: The command, or the list of commands, it is for.
    command: str
    #: What it asked for — `task` in `{input.task}`.
    name: str
    #: What was already given for the same run.
    values: dict[str, str]


@dataclass(frozen=True)
class StepRounds:
    """A workflow step's rounds: the ones it has had, how many it may, and how
    to count the one on its way once the seat takes it — and which step of which
    run it is, for the inspections it runs to be kept against."""

    before: tuple[flowing.Round, ...]
    allowed: int
    count: Callable[[str], Awaitable[int]]
    #: The run, as `workflow_steps` knows it, the step's name, and its phase
    #: when it is one of a flow's phase steps.
    run: str = ""
    step: str = ""
    phase: int | None = None


@dataclass(frozen=True)
class _SessionTarget:
    """A resolved session together with the runtime that owns it.

    Keeping these together matters. A session id is only meaningful to its
    runtime: handing a Codex id to Claude Code produces the very plausible
    "No conversation found" error that hid this boundary leak.
    """

    session_id: str
    project: str
    cwd: str | None
    runner: AgentRunner


def _phase_named(typed: str) -> tuple[int | None, str]:
    """`phase 2` at the start of what followed a step's name, and the rest.

    Only a whole number of at least one reads as a phase; anything else is the
    note it always was.
    """
    word, _, rest = typed.strip().partition(" ")
    number, _, after = rest.strip().partition(" ")
    if word.casefold() == "phase" and number.isdigit() and int(number) >= 1:
        return int(number), after.strip()
    return None, typed


def parse_destination(value: str | None) -> tuple[str, int | None] | None:
    """Read `chat_id` or `chat_id:thread_id` into where a message goes.

    One syntax for both shapes a group can take: a chat of its own, or a forum
    topic inside a shared one. Which you want is a matter of how you like your
    phone organised, and not something the code should have an opinion about.
    """
    if not value:
        return None
    chat, _, thread = value.rpartition(":")
    if chat and thread.isdigit():
        return chat, int(thread)
    return value, None


class _SeatDelivery:
    """The channel's side of `handoffs.Delivery`: the path `/to` takes.

    So a handoff lands where a person would have sent it by hand, and both chats
    say so, the way they do for `/to`. `accepted` runs once the seat's session
    takes the message, which is when a handoff's round counts.
    """

    def __init__(
        self,
        channel: TelegramChannel,
        actor: str,
        chat_id: str,
        thread_id: int | None,
        accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._channel = channel
        self._actor = actor
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._accepted = accepted

    async def to_seat(self, label: str, text: str) -> None:
        await self._channel._forward_to_seat(
            f"{label} {text}",
            self._actor,
            self._chat_id,
            self._thread_id,
            accepted=self._accepted,
        )


def _files_of(said: last_said.Said | None) -> frame.Tree | None:
    """Where the files stood when a reply came in, if that was recorded."""
    if said is None or not said.content:
        return None
    return frame.Tree(head=said.head or "?", content=said.content)


def _ended(result: commands_running.Result) -> str:
    """How a command ended, in a word."""
    if result.timed_out:
        return "stopped"
    return "passed" if result.ok else "failed"


@dataclass
class _Inspection:
    """An inspection's turn while it runs: what it is, where its answer is going,
    and the task running it — which is what Stop cancels."""

    name: str
    destination: tuple[str, int | None]
    turn: asyncio.Future
    #: Who pressed Stop, once somebody has.
    stopped_by: str | None = None


class _Inspecting:
    """The channel's side of `inspections.Asker`: a runtime's turn, known by its id.

    Each turn starts under an id chosen here, kept with what the inspection is
    and where its answer is going for as long as it runs. A command the
    inspection asks to run is then a card that says which inspection wants it,
    in the chat the answer is headed for — rather than one from a session nobody
    has seen, in whichever chat an unknown session falls to. The card can stop
    the inspection.
    """

    def __init__(
        self,
        channel: TelegramChannel,
        runner,
        destination: tuple[str, int | None],
        project: str | None = None,
    ) -> None:
        self._channel = channel
        self._runner = runner
        self._destination = destination
        self._project = project

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
    ) -> str | None:
        # The inspection's own id when it chose one, so its record and its
        # tokens are found by the same key.
        session = session_id or str(uuid.uuid4())
        running = _Inspection(
            name or "inspection",
            self._destination,
            asyncio.ensure_future(
                self._runner.ask(
                    text,
                    timeout=timeout,
                    model=model,
                    cwd=cwd,
                    edits=edits,
                    session_id=session,
                    purpose=f"inspect {name}" if name else "inspect",
                    project=self._project,
                    effort=effort,
                )
            ),
        )
        self._channel._inspecting[session] = running
        logger.info("Inspection %s runs as session %s", running.name, session)
        try:
            return await running.turn
        except asyncio.CancelledError:
            if running.stopped_by is None:
                raise
            raise inspecting.StoppedError(f"stopped by {running.stopped_by}") from None
        finally:
            self._channel._inspecting.pop(session, None)

    @property
    def runtime(self) -> str:
        """Which runtime the turns run on — `claude-code`."""
        return str(getattr(self._runner, "id", "") or "")


class _Keeping:
    """The channel's side of `inspections.Keeper`: a finished inspection run into
    the database, with the project, the work and — for a workflow's step — the
    step it ran for. Only made when `keep_inspections` is on; nothing that fails,
    and nothing that holds anybody up."""

    def __init__(
        self,
        channel: TelegramChannel,
        *,
        project: str,
        work: str | None,
        runtime: str,
        counted: StepRounds | None = None,
    ) -> None:
        self._channel = channel
        self._project = project
        self._work = work
        self._runtime = runtime
        self._counted = counted

    async def keep(self, kept: inspecting.Kept) -> None:
        """Written off to one side, as a label is: the answer is on its way to
        somebody, and a row in a database shares its file with the audit log,
        which may be writing. Nothing waits for it."""
        database = self._channel._database
        if database is None:
            return
        step = self._counted
        write = asyncio.to_thread(
            partial(
                inspecting.record.keep,
                database,
                kept,
                project=self._project,
                work=self._work,
                runtime=self._runtime or None,
                workflow_run=step.run or None if step else None,
                step=step.step or None if step else None,
                phase=step.phase if step else None,
                round=len(step.before) + 1 if step else None,
            )
        )
        self._channel._detach(write, f"keep inspection {kept.name}")


class _Labelling:
    """The channel's side of `inspections.Labeller`: a label onto this project's task.

    Written off to one side, so an inspection's answer and a handoff never wait on an
    issue tracker, and quietly — the log says what happened, nobody is asked.
    """

    def __init__(self, channel: TelegramChannel, found: Project) -> None:
        self._channel = channel
        self._found = found

    async def label(self, label: str) -> None:
        self._channel._detach(self._channel._put_label(self._found, label), f"label {label}")


class _Running:
    """The channel's side of `handoffs.Runner`: a project command, run where the
    project is, with how it is getting on said in the chat the handoff came from.

    The command says for itself that it started and how it ended, in the log —
    see `commands.running`. What this adds is what only the channel can: the
    chat that is waiting sees it move.
    """

    def __init__(
        self, channel: TelegramChannel, path: Path, chat_id: str, thread_id: int | None
    ) -> None:
        self._channel = channel
        self._path = path
        self._chat_id = chat_id
        self._thread_id = thread_id

    async def run(self, command: commands_offered.Command) -> commands_running.Result:
        loop = asyncio.get_running_loop()

        def progress(seconds: float, latest: str) -> None:
            asyncio.run_coroutine_threadsafe(
                self._channel._say(
                    f"… <b>{html.escape(command.name)}</b> "
                    f"{self._channel._elapsed(seconds)} — "
                    f"<code>{html.escape(latest[:150])}</code>",
                    self._chat_id,
                    self._thread_id,
                ),
                loop,
            )

        return await asyncio.to_thread(
            partial(
                commands_running.run,
                command.line,
                self._path,
                timeout=HANDOFF_COMMAND_TIMEOUT_SECONDS,
                on_progress=progress,
            )
        )


class TelegramChannel:
    """Puts approvals in a chat and brings the answers back."""

    def __init__(
        self,
        *,
        api: TelegramApi,
        store: ApprovalStore,
        audit: AuditLog,
        chat_id: str,
        question_store: QuestionStore | None = None,
        authorized_user_ids: frozenset[str],
        clock: Clock = _default_clock,
        poll_retry_seconds: float = POLL_RETRY_SECONDS,
        gate: Gate | None = None,
        project: str = "unknown",
        navigator_chat_id: str | None = None,
        driver_chat_id: str | None = None,
        registry: SessionRegistry | None = None,
        runner=None,
        runners: dict[str, object] | None = None,
        seats: list[Seat] | None = None,
        session_names: dict[Role, str] | None = None,
        prompts: Mapping[str, str] | None = None,
        repositories: Mapping[str, Project] | None = None,
        forge_token: str | None = None,
        #: Where the last thing said in each chat is kept, so `/forward`
        #: survives a restart. None disables it: nothing else depends on it.
        said_path: Path | None = None,
        #: The database the audit log and turn usage share, where a finished
        #: workflow run is kept for reading later. None keeps none.
        database: Path | None = None,
        #: Whether every inspection run is kept there too, text and all. Off
        #: unless asked for — see `Settings.keep_inspections`.
        keep_inspections: bool = False,
        #: The model inspections run on, and how hard it thinks, unless a
        #: project's own entry for an inspection says otherwise. What it leaves
        #: unsaid is `INSPECTION_MODEL`, at the runtime's own effort.
        inspection_model: ModelChoice | None = None,
        #: Each runtime's own availability-check context, by runtime name — see
        #: `RuntimeSpec.check_context`. Asked only when a seat's session cannot
        #: be found, to say *why*; each check is handed its own and nobody else's.
        check_contexts: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self._api = api
        self._check_contexts = {name: dict(c) for name, c in (check_contexts or {}).items()}
        self._gate = gate or Gate()
        self._project = project
        self._store = store
        self._audit = audit
        self._chat_id = chat_id
        self._said_path = said_path
        self._database = database
        self._keep_inspections = keep_inspections
        self._inspection_model = (inspection_model or ModelChoice()).over(
            ModelChoice(INSPECTION_MODEL)
        )
        # Two seats and a default. A role with nowhere of its own falls back to
        # the main chat, so an existing single-chat setup keeps working
        # untouched by any of this.
        self._routes = {
            Role.NAVIGATOR: parse_destination(navigator_chat_id),
            Role.DRIVER: parse_destination(driver_chat_id),
        }
        # Four groups where there used to be two: a navigator and a driver for
        # each runtime. Which one a card belongs to is (role, runtime), and
        # both halves already travel with the request — the hook says what the
        # session is called and `agent_id` says what ran it. Nothing is chosen
        # at the moment of sending; the configuration decides, as it did
        # before, and there is simply more of it.
        self._seats = list(seats or [])
        # Each project as configured — where its code is, and what has to pass
        # before a commit is offered from it. Empty is a fine state: `/commit`
        # then says it does not know rather than guessing, and every other
        # command is unaffected.
        self._repositories = dict(repositories or {})
        self._registry = registry
        # One runner per runtime, shared by every seat that uses it.
        self._runners = dict(runners or {})
        self._runner = runner or next(iter(self._runners.values()), None)
        self._session_names = session_names or {}
        # Sentences somebody says often enough to want a name for. Configured,
        # so changing the wording of one is editing a file rather than waiting
        # for a release.
        self._prompts = dict(prompts if prompts is not None else configured_prompts.DEFAULTS)
        self._sending: set[asyncio.Task] = set()
        # Who pressed a seat button and has not yet said what to send. Keyed by
        # the person as well as the chat: in a group, somebody else typing must
        # not be swept into a hand-off they did not ask for.
        self._handoffs: dict[tuple[str, int | None, str], tuple[str, datetime]] = {}
        self._authorized = authorized_user_ids
        self._clock = clock
        #: For reaching an issue tracker. Absent means `/label` says so and
        #: nothing else notices.
        self._forge_token = forge_token
        #: Which command is running in which project, so a second one is
        #: refused rather than started on top of it.
        self._working: dict[str, str] = {}
        #: Labels somebody picked for a command, by project and work item — the
        #: value it runs with when the task could not be given the label, or
        #: before the tracker says it has it. Gone on a restart: the task is
        #: where a pick is kept for good.
        self._picked: dict[tuple[str, str], dict[str, str]] = {}
        #: A value a command asked for, by the chat and thread it was asked in,
        #: with when — the next message there answers it, for a few minutes.
        self._inputs: dict[tuple[str, int | None], tuple[_Asked, datetime]] = {}
        #: Commits proposed and not yet answered. Owned by `halyard.commits`,
        #: which is where taking-once and going-stale are decided.
        self._proposals = commits.Proposals(self._clock)
        self._poll_retry_seconds = poll_retry_seconds
        # The chat is remembered alongside the message, because a card that
        # was routed to a seat has to be edited in that seat. Keeping only
        # the message id means editing against the wrong chat, which fails
        # quietly and leaves live-looking buttons on a settled question.
        #: Open cards, by handle: the request, the message, the chat it is
        #: in, and the forum topic within it. The topic used to be worked
        #: out at send time and thrown away, so a reply to a button had to
        #: derive the destination again — and could disagree with where the
        #: card actually is.
        self._open: dict[str, tuple[ApprovalRequest, int, str, int | None]] = {}
        #: The inspections running now, by the session id each was started
        #: under: what the inspection is, where its answer is headed — which is
        #: where a command it asks to run goes — and how to stop it. See
        #: `_Inspecting`.
        self._inspecting: dict[str, _Inspection] = {}
        # Questions are held apart from approvals: a different store answers
        # them, and their button carries an option index rather than allow/deny.
        # Same shape otherwise — handle to (request, message id, chat).
        self._question_store = question_store
        self._open_questions: dict[str, tuple[QuestionRequest, int, str]] = {}
        self._poller: asyncio.Task | None = None
        self._offset: int | None = None
        # Which chat the command being handled came from, so a listing can mark
        # the seat you are already standing in.
        self._here: str = ""

    @property
    def name(self) -> str:
        return "telegram"

    def _menu(self) -> tuple[tuple[str, str], ...]:
        """Every command this bot answers: the built-in ones, then yours.

        One list, used both for the menu Telegram publishes and for `/help`.
        Two lists would drift, and the way you would find out is by typing a
        command the menu offered and being ignored.
        """
        return COMMANDS + tuple(
            (name, configured_prompts.describe(text)) for name, text in self._prompts.items()
        )

    async def start(self) -> None:
        await self._api.open()
        # Registered so they appear when somebody types `/`. Best-effort: the
        # bot answers every one of these whether Telegram knows about them or
        # not, and a control plane that would not start because a menu could
        # not be published would be trading the thing for the label on it.
        try:
            await self._api.set_my_commands(self._menu())
        except Exception:
            logger.warning("Could not register the command list with Telegram", exc_info=True)
        self._poller = asyncio.create_task(self._poll_forever(), name="telegram-poll")

    async def stop(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            # Awaiting the cancelled task is what makes stop() actually wait for
            # the poll to unwind, rather than returning while it is still alive.
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None
        await self._api.close()

    # --- sending ------------------------------------------------------------

    def _route(
        self,
        role: Role | None,
        session_name: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        project: str | None = None,
    ) -> tuple[str, int | None]:
        """Where this seat's traffic goes.

        By whatever the seat was configured with — a name or a session id.
        A role stopped being enough the moment a second runtime arrived: a
        Claude driver and a Codex driver are both `driver`, and their cards
        belong in different groups.

        Both are accepted because they fail differently. A name is readable and
        is what you would copy out of an app, and it can be changed there
        without anybody remembering that a seat pointed at it. An id is
        unreadable and permanent. Matching either means a configuration written
        with one is not quietly wrong once somebody uses the other.

        **Always with the runtime.** The address is `(runtime, session)`, and
        `for_session` is where that is enforced — matching a bare name sent
        Antigravity's reply into the Claude driver's group, because both seats
        are named `alpha-engine-driver` and the Claude one is listed first.

        Then by role and runtime, then by role alone, then the default chat —
        so a setup with two seats keeps behaving exactly as it did.
        """
        owner = for_session(self._seats, agent_id, session_name, session_id)
        if owner is None:
            # A runtime whose sessions have no name it could be addressed by.
            # opencode is the case: an id nobody types and a title it writes
            # from the conversation, so what identifies the seat is the
            # codebase. Answers nothing when two seats of one runtime share a
            # project, because guessing between them is the mistake the
            # paragraph above is about.
            owner = for_project(self._seats, agent_id, project)
        if owner is not None and owner.chat:
            return parse_destination(owner.chat) or (self._chat_id, None)
        # Only when a role was actually declared. Falling back on `None`
        # matches any seat that also has no role, so a seat with nowhere of its
        # own to speak would borrow the group of an unrelated one — measured
        # with seven seats, where a scratch seat's card landed in a reviewer's
        # group. No destination should mean the default chat, not somebody
        # else's.
        if role is not None:
            for seat in self._seats:
                if (
                    seat.role is role
                    and seat.chat
                    and (agent_id is None or seat.runtime == agent_id)
                ):
                    return parse_destination(seat.chat) or (self._chat_id, None)
        return (role and self._routes.get(role)) or (self._chat_id, None)

    async def send_approval_request(self, request: ApprovalRequest) -> str:
        """Put a card in the chat.

        Raising propagates to the service, which denies. That is the right
        outcome: an approval that never reached anybody is not an approval, and
        the alternative is a bridge blocked on a question nobody was asked.
        """
        self._forget_expired()
        # An inspection's turn is nobody's seat. Its command goes where the
        # inspection's answer is going, says it is an inspection's, and can stop
        # the inspection; routed
        # like a seat's, it would land wherever an unknown session falls, over
        # a session id nobody has seen.
        running = self._inspecting.get(request.session_id)
        inspection = running.name if running else None
        text = cards.render(request, now=self._clock(), inspection=inspection)
        markup = cards.keyboard(
            request,
            include_full=request.command_full != request.command_summary,
            stoppable=running is not None,
        )
        chat_id, thread_id = (running.destination if running else None) or self._route(
            request.role,
            request.session_name,
            request.agent_id,
            request.session_id,
            request.project,
        )
        message = await self._api.send_message(
            chat_id, text, reply_markup=markup, message_thread_id=thread_id
        )
        message_id = int(message["message_id"])
        self._open[cards.handle_of(request)] = (request, message_id, chat_id, thread_id)
        # Where a card went, said once per card. "It arrived in the wrong
        # group" is otherwise a question only the person holding the phone can
        # answer, and the answer decays as soon as they scroll.
        logger.info(
            "Card for %s (%s, session %r) sent to %s",
            request.project,
            request.agent_id,
            f"inspection {inspection}" if inspection else request.session_name or "unnamed",
            chat_id,
        )
        return str(message_id)

    async def close_approval(self, request: ApprovalRequest, *, decision: str, by: str) -> None:
        """Take the buttons off a card whose question was answered somewhere else.

        The runtime's own prompt won the race — somebody at the desk answered
        first — and a card left live would go on asking a settled question until
        it expired. It says who answered, and how, instead.
        """
        entry = self._open.get(cards.handle_of(request))
        if entry is None:
            return
        _, message_id, chat_id, _ = entry
        await self._settle_card(request, message_id, chat_id, decision, by)

    async def send_question(self, request: QuestionRequest) -> str:
        """Put a question card in the seat's chat.

        Routed exactly like an approval — a question from a Codex driver and one
        from a Claude driver belong in different places for the same reason a
        card does. Raising propagates to the service, which falls back to the
        terminal picker; a question that reached nobody is not one to block on.
        """
        self._forget_expired_questions()
        text = cards.render_question(request, now=self._clock())
        markup = cards.question_keyboard(request)
        chat_id, thread_id = self._route(
            request.role,
            request.session_name,
            request.agent_id,
            request.session_id,
            request.project,
        )
        message = await self._api.send_message(
            chat_id, text, reply_markup=markup, message_thread_id=thread_id
        )
        message_id = int(message["message_id"])
        self._open_questions[cards.question_handle_of(request)] = (request, message_id, chat_id)
        logger.info(
            "Question for %s (%s, session %r) sent to %s",
            request.project,
            request.agent_id,
            request.session_name or "unnamed",
            chat_id,
        )
        return str(message_id)

    async def send_message(
        self,
        session_id: str,
        text: str,
        role: Role | None = None,
        *,
        agent_id: str | None = None,
        session_name: str | None = None,
        project: str | None = None,
    ) -> str:
        """Send an agent's own words, split across messages if they are long.

        Escaped, because this is somebody else's prose: a reply mentioning a
        `<div>` is not markup, and sending it as markup makes Telegram refuse
        the whole message.
        """
        chat_id, thread_id = self._route(role, session_name, agent_id, session_id, project)
        # Kept before the split, because what somebody wants to hand on is the
        # whole report and a fragment of one would look complete.
        if (said_path := self._kept(chat_id, SAID_FILE)) is not None:
            files = await self._files_at_reply(project, agent_id, session_id)
            last_said.remember(
                said_path,
                chat_id=chat_id,
                text=text,
                session_id=session_id,
                agent_id=agent_id,
                head=files.head if files else None,
                content=files.content if files else None,
            )
        # A workflow waiting on this seat takes its next step — off this path,
        # which is relaying a reply: the step ahead may run commands and inspections,
        # and none of that should hold up what the seat just said.
        answered = for_session(self._seats, agent_id, session_name, session_id) or for_project(
            self._seats, agent_id, project
        )
        if answered is not None:
            self._detach(self._advance_workflow(answered, text), "/workflow")
        # A provider that has stopped answering is the one message worth a
        # button. Somebody reading "the limit resets at 03:30" on a phone can
        # do exactly one useful thing about it, and typing a model id with a
        # slash in it is not how they should have to do it.
        keyboard = self._offer_another_model(text, agent_id)

        chunks = cards.split_for_telegram(text)
        message = None
        for index, chunk in enumerate(chunks, start=1):
            marker = f"<i>({index}/{len(chunks)})</i>\n" if len(chunks) > 1 else ""
            message = await self._api.send_message(
                chat_id,
                marker + html.escape(chunk),
                message_thread_id=thread_id,
                # On the last one, where somebody's thumb already is.
                reply_markup=keyboard if index == len(chunks) else None,
            )
        return str(message["message_id"]) if message else ""

    def _offer_another_model(self, text: str, agent_id: str | None) -> dict | None:
        """The fallback model as a button, when a provider has stopped answering.

        Offered rather than switched. The other model costs different money and
        may be worse at the work in hand; that is a decision, and this system
        does not make those on somebody's behalf.

        `None` whenever anything is missing — no marker, no configuration, no
        fallback named. Every one of those means there is nothing useful to
        offer, and a button that sets a model nobody chose would be worse than
        no button.
        """
        if not text.startswith(OUT_OF_QUOTA) or not agent_id:
            return None
        try:
            from halyard.core.config_file import runtime_settings

            configured = runtime_settings().get(agent_id)
        except Exception:
            logger.debug("Could not read `runtimes:` for %s", agent_id, exc_info=True)
            return None
        if configured is None or not configured.on_quota:
            return None
        return cards.choices("model", (configured.on_quota,))

    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role: Role | None = None,
        *,
        agent_id: str | None = None,
        session_name: str | None = None,
    ) -> str:
        """Send something that will not fit in a message.

        As a file rather than a wall of split messages: a diff or a full command
        is something you want to be able to scroll and search, not reassemble
        from six chat bubbles.
        """
        chat_id, thread_id = self._route(role, session_name, agent_id, session_id)
        return await self._put_long_content(chat_id, thread_id, content, title)

    async def _put_long_content(
        self, chat_id: str, thread_id: int | None, content: str, title: str
    ) -> str:
        """The same, to a destination already known.

        Split out for the one caller that has no routing question to ask: a
        button pressed on a card belongs to the chat that card is in. Working
        the destination out again can only disagree with where somebody is
        looking — and did, sending the full command of a card in one group into
        another.
        """
        if len(content) <= cards.MESSAGE_LIMIT - 200:
            message = await self._api.send_message(
                chat_id,
                f"<b>{html.escape(title)}</b>\n<pre>{html.escape(content)}</pre>",
                message_thread_id=thread_id,
            )
            return str(message["message_id"])
        filename = f"{title.lower().replace(' ', '-')}.txt"
        result = await self._api.send_document(
            chat_id,
            filename,
            content.encode("utf-8"),
            caption=title,
            message_thread_id=thread_id,
        )
        return str(result["message_id"])

    # --- listening ----------------------------------------------------------

    async def _poll_forever(self) -> None:
        """Keep asking Telegram for updates until cancelled.

        Survives errors. If this loop stopped, no approval could ever be
        answered — they would all sit until their deadline and then be denied.
        That is the safe failure, but it is silent, so a poll that keeps failing
        says so at ERROR rather than letting the system look healthy.

        It says so *once*, though. The first failure logs a full traceback,
        because you need it to know what broke; every consecutive one after that
        logs a single line with a running count. A transient DNS failure that
        printed eighteen identical tracebacks is what prompted this — the noise
        made a recoverable blip look like a crash.
        """
        failures = 0
        while True:
            try:
                updates = await self._api.get_updates(
                    offset=self._offset, timeout=POLL_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures == 1:
                    logger.exception(
                        "Telegram poll failed; approvals cannot be answered until it recovers"
                    )
                else:
                    logger.error("Telegram poll still failing (%d consecutive): %s", failures, exc)
                await asyncio.sleep(self._backoff(failures))
                continue

            if failures:
                # Say so explicitly. Errors stopping is not something anyone
                # notices in a log; a line saying they stopped is.
                logger.warning("Telegram poll recovered after %d consecutive failures", failures)
                failures = 0

            for update in updates:
                self._offset = int(update["update_id"]) + 1
                try:
                    if callback := update.get("callback_query"):
                        await self._handle_callback(callback)
                    elif message := update.get("message"):
                        await self._handle_message(message)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad update must not take the loop down with it, or a
                    # single malformed message would silence every future answer.
                    logger.exception("Failed to handle a Telegram update")

    async def _handle_message(self, message: dict) -> None:
        """Handle a typed command.

        The same authorization as a button press, for the same reason: closing
        the gate stops anyone being asked, and a stranger must not be able to do
        that any more than they can approve something.

        Silent for anything that is not a command. A chat where the bot argues
        with every stray message is a chat nobody keeps notifications on for,
        and notifications are the entire point.
        """
        text = (message.get("text") or "").strip()
        if not text:
            return

        user_id = str((message.get("from") or {}).get("id", ""))
        if user_id not in self._authorized:
            await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
            logger.warning("Ignoring a message from unauthorized Telegram user %s", user_id)
            return

        actor = f"tg:{user_id}"
        here = str((message.get("chat") or {}).get("id") or "") or None
        thread = message.get("message_thread_id")

        if not text.startswith("/"):
            # A question open in this chat takes the message as its answer — the
            # "Other" path. The session is blocked waiting on it, so this is what
            # the words are for; a normal forward could not run anyway. Checked
            # before anything else for that reason.
            if here and await self._answer_open_question_with_text(here, text, user_id):
                return
            # A value a command asked for takes the next message in its chat,
            # before a seat could: it was asked for a moment ago, right here.
            if here and await self._answer_input(here, thread, text):
                return

            replied = (message.get("reply_to_message") or {}).get("text") or ""
            # Two ways to know this sentence belongs to a seat that was picked
            # a moment ago, and either is enough. The reply carries the question
            # itself, which is exact; the pending hand-off is what remains when
            # the client did not attach it. Relying on the reply alone put two
            # messages in front of an agent nobody had chosen.
            # A reply to a commit prompt is that commit's message, not a
            # sentence for a session. Taken before the seat hand-off below,
            # which would otherwise claim it and send it to an agent.
            if (waiting := _commit_being_asked_for(replied)) is not None:
                await self._rewrite_commit(waiting, text, here or "", thread, user_id)
                return

            answering = _seat_being_asked_for(replied) or self._take_handoff(here, thread, user_id)
            # The line that was missing while this was being guessed at: where
            # the message went, and what it arrived attached to.
            logger.info(
                "Message from %s → %s (replying to %r)",
                actor,
                f"seat {answering}" if answering else "this chat's own seat",
                replied[:60],
            )
            if answering:
                await self._forward_to_seat(f"{answering} {text}", actor, here or "", thread)
                return
            await self._forward_to_session(text, actor, here or "", thread)
            return

        # Any command means the sentence never came. Dropping the hand-off here
        # keeps it from attaching itself to something typed much later.
        self._take_handoff(here, thread, user_id)

        command, _, argument = text.partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        argument = argument.strip()

        # Every command, as it arrives. Nothing on this path was logged at all,
        # which is why a `/commit` that seemed to do nothing left no trace to
        # look at afterwards — on either machine, by either of us. A command
        # that reached Halyard and a command that never did are the first two
        # things to tell apart, and the log could not tell them apart.
        logger.info("Command /%s from %s in %s", command, actor, here or "the default chat")

        if command == "chat":
            # An explicit way to say the same thing as plain text. Worth having:
            # a bot in a group sees only commands while privacy mode is on, and
            # leaving that on is a reasonable thing to want.
            if not argument:
                await self._say("Usage: <code>/chat &lt;message&gt;</code>", here, thread)
                return
            await self._forward_to_session(argument, actor, here or "", thread)
            return
        if command == "to":
            # What is being handed over: the message this replies to, or the
            # rest of the line. Replying is the natural gesture for "this one,
            # over there", and it means the text never has to be retyped.
            reply_to = message.get("reply_to_message") or {}
            replied = (reply_to.get("text") or "").strip()
            # Whichever message holds the text is what the buttons hang off.
            anchor = reply_to.get("message_id") if replied else message.get("message_id")
            await self._forward_to_seat(
                argument, actor, here or "", thread, replied=replied, anchor_id=anchor
            )
            return
        if command == "forward":
            await self._forward_last(argument, actor, here or "", thread)
            return
        if command in ("inspect", "checks"):
            # `/checks` is what it was called until 2026-09-23, and still works.
            # Detached: each inspection is a model turn of its own over a whole
            # report, and the poller has everybody else's buttons to answer.
            self._detach(self._run_inspection(argument, here or "", thread), "/inspect")
            return
        if command == "workflow":
            # Detached like `/handoff`, and for longer: this one takes a step
            # that may run commands and inspections, and then waits for a seat.
            self._detach(self._run_workflow(argument, here or "", thread, actor), "/workflow")
            return
        if command == "handoff":
            # Detached for the same reason: a handoff may run inspections first.
            self._detach(self._run_handoff(argument, here or "", thread, actor), "/handoff")
            return
        if command in ("commit", "review_and_commit"):
            # Detached: this reads a repository, may run the project's whole
            # gate, and asks a model for a sentence. None of that may hold the
            # poller, which is what answers everybody else's buttons.
            self._detach(
                self._propose_commit(here or "", thread, full=command != "commit"),
                f"/{command}",
            )
            return
        if command == "open":
            await self._open_application(argument, here or "", thread)
            return
        if command == "command":
            await self._run_command(argument, here or "", thread)
            return
        if command == "doctor":
            # Detached: it shells out to every runtime's CLI and reads two
            # repositories, which is seconds rather than milliseconds.
            self._detach(self._report_health(here or "", thread), "/doctor")
            return
        if command == "label":
            # Detached for the same reason: two calls to an issue tracker over
            # somebody else's network.
            self._detach(self._label_task(argument, here or "", thread), "/label")
            return
        if command == "pause":
            _, changed = await self._gate.pause(actor)
            if changed:
                await self._record(gate_changed(paused=True, actor=actor, project=self._project))
            await self._say(
                (
                    "⏸ <b>Paused.</b> Halyard steps out of the way — no approval "
                    "cards, no replies. Claude Code decides on its own again, "
                    "exactly as if the hook were not installed: whatever its "
                    "<code>permissions.allow</code> list covers runs without "
                    "asking anybody, and the rest it asks you at the desk."
                )
                if changed
                else "⏸ Already paused.",
                here,
                thread,
            )
        elif command == "resume":
            _, changed = await self._gate.resume(actor)
            if changed:
                await self._record(gate_changed(paused=False, actor=actor, project=self._project))
            await self._say(
                "▶️ <b>Resumed.</b> Approvals are coming back here."
                if changed
                else "▶️ Already running.",
                here,
                thread,
            )
        elif command in ("model", "effort"):
            await self._choose(command, argument, here, thread)
        elif command == "options":
            await self._say(self._options(here), here, thread)
        elif command == "status":
            await self._say(await self._status(), here, thread)
        elif command in self._prompts:
            # A sentence somebody says often enough to have named. Whatever
            # follows the command is added to it, so `/md the failing test`
            # arrives as the prompt with that on the end.
            said = self._prompts[command]
            await self._forward_to_session(
                f"{said}\n\n{argument}" if argument else said, actor, here or "", thread
            )
        elif command in ("start", "help"):
            listed = "\n".join(f"/{name} — {description}" for name, description in self._menu())
            await self._say(
                "<b>Halyard</b>\n\nType anything to send it into the session.\n\n" + listed,
                here,
                thread,
            )

    def _seat_list(self) -> str:
        """Every seat that can be addressed, as a person would pick one from.

        Printed whenever a label is missing or wrong, rather than kept in a
        separate `/seats` command — the moment you need the list is the moment
        you got the name wrong, and a second command to go and look it up is a
        second thing to remember.
        """
        if not self._seats:
            return "No seats are configured."
        lines = []
        for seat in self._seats:
            where = " (this chat)" if seat.chat and seat.chat.split(":")[0] == self._here else ""
            lines.append(
                f"  <code>{html.escape(seat.label)}</code> — "
                f"{html.escape(seat.runtime)}"
                f"{' · ' + html.escape(seat.session) if seat.session else ''}{where}"
            )
        return "Seats you can send to:\n" + "\n".join(lines)

    async def _forward_to_seat(
        self,
        argument: str,
        actor: str,
        chat_id: str,
        thread_id: int | None = None,
        replied: str = "",
        anchor_id: int | None = None,
        accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Send a message to a seat by name, from anywhere.

        Eight groups is what a person ends up with — a navigator and a driver
        per runtime, per machine — and each one talks to exactly one session.
        Handing a question to a different agent meant finding its group and
        retyping the question in it.

        This is the second way to reach a seat, and the one `seats.py` was
        written for: *"any seat can be named explicitly from anywhere, which is
        what makes it possible to take what one seat just wrote and hand it to
        another."* The lookup has been there since; nothing called it.

        **The reply still goes to the seat's own chat, not this one.** That is
        the property worth keeping: a seat's conversation stays in one place,
        readable from top to bottom, rather than being split across whichever
        group somebody happened to be standing in.
        """
        self._here = chat_id
        label, _, typed = argument.partition(" ")
        label, typed = label.strip(), typed.strip()
        # What was typed wins over what was replied to: somebody who wrote out
        # a message meant that one, and silently sending the other instead
        # would be the worst kind of helpful.
        #
        # And what was replied to is dropped when it is one of Halyard's own
        # questions. Those sit in the chat looking like messages, so replying
        # `/to nav` to one would hand an agent the question rather than an
        # answer — which is how "Send what to nav?" reached a navigator.
        text = typed or ("" if _is_our_own_prompt(replied) else replied)

        if not text and not label:
            # Nothing to send and no seat named: ask which, and the press will
            # ask for the message. Two taps, no memory.
            await self._offer_seats("", chat_id, thread_id, anchor_id)
            return

        if not text:
            await self._ask_for_text(label, chat_id, thread_id, actor.removeprefix("tg:"))
            return

        if not label:
            # The text is known and the seat is not, which is the one case a
            # button can finish on its own — pressing it needs nothing
            # remembered, because the message it applies to is the one the
            # buttons are attached to.
            await self._offer_seats(text, chat_id, thread_id, anchor_id)
            return

        seat = find(self._seats, label)
        if seat is None:
            await self._say(
                f"No seat called <b>{html.escape(label)}</b>.\n\n" + self._seat_list(),
                chat_id,
                thread_id,
            )
            return

        destination, destination_thread = parse_destination(seat.chat) or (chat_id, thread_id)
        # Said in both places on purpose. The person sending it is not looking
        # at the seat they sent it to, and the seat's own chat should not
        # acquire a message from nowhere.
        await self._say(
            f"→ sent to <b>{html.escape(seat.label)}</b> ({html.escape(seat.runtime)})",
            chat_id,
            thread_id,
        )
        if destination != chat_id:
            # In pieces Telegram will take, the way a relayed reply is sent.
            # Whole, a handoff — the project's prompt and a report — ran past
            # 4096 characters, Telegram refused it, and the refusal went to the
            # log: the session got the message and its chat showed nothing.
            chunks = cards.split_for_telegram(text)
            for index, chunk in enumerate(chunks, start=1):
                marker = f"<i>({index}/{len(chunks)})</i>\n" if len(chunks) > 1 else ""
                head = (
                    f"↪ from <b>{html.escape(actor)}</b>, via another chat:\n\n"
                    if index == 1
                    else ""
                )
                await self._say(head + marker + html.escape(chunk), destination, destination_thread)
        await self._forward_to_session(
            text, actor, destination, destination_thread, accepted=accepted
        )

    def _detach(self, work, what: str) -> None:
        """Run something slow without holding the poll loop.

        Updates are handled one at a time, in the loop that fetches them — which
        is right for everything that answers in milliseconds and wrong for
        anything that shells out. A `/commit` running a project's test suite
        parked the poller for minutes: cards kept arriving, because those are
        sent from the HTTP side, and not one of them could be *answered*. From a
        phone that is indistinguishable from Halyard being down, and it is worse,
        because the approvals expire while it looks alive.
        """
        task = asyncio.create_task(work, name=what)
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)
        task.add_done_callback(lambda done: self._said_if_it_broke(done, what))

    @staticmethod
    def _said_if_it_broke(done: asyncio.Task, what: str) -> None:
        """A detached failure is silent unless somebody looks at it."""
        with contextlib.suppress(asyncio.CancelledError):
            if (failed := done.exception()) is not None:
                logger.error("%s failed", what, exc_info=failed)

    def _remember_handoff(self, seat: str, chat_id: str, thread_id: int | None, user: str) -> None:
        """Note that this person picked a seat and owes us a sentence."""
        self._handoffs[(chat_id, thread_id, user)] = (seat, self._clock())

    def _take_handoff(self, chat_id: str | None, thread_id: int | None, user: str) -> str | None:
        """The seat this person picked, if they picked one recently enough.

        Taken rather than read: a hand-off covers the next thing said and
        nothing after it. Leaving it in place would mean a tap made once
        quietly redirecting a conversation.
        """
        found = self._handoffs.pop((chat_id or "", thread_id, user), None)
        if found is None:
            return None
        seat, when = found
        if self._clock() - when > timedelta(seconds=HANDOFF_SECONDS):
            return None
        return seat

    async def _ask_for_text(
        self, label: str, chat_id: str, thread_id: int | None, user: str
    ) -> None:
        """Ask what to send.

        Without `force_reply`, which this used to carry. It asks the client to
        aim the next message at this one, so the answer arrives with the
        question attached and the question names the seat — the exact path, and
        it worked. What it also did was leave the reply box open on a question
        nobody answered, and Telegram keeps offering it: reported as coming
        back after closing it repeatedly, and after restarting the client.
        A prompt that will not go away costs more than the precision it buys.

        Both paths that read the answer are still here. Replying to this by
        hand still carries the question — `force_reply` only opened the box, it
        never created the link — and the seat is held for a few minutes besides.
        What is lost is the box opening itself, which is a convenience; what is
        gained is that abandoning the question costs nothing.

        `/forward` is the other half of this, and needs none of it: it has
        nothing to ask for, so it offers buttons and finishes on the tap.
        """
        seat = find(self._seats, label)
        if seat is None:
            await self._say(
                f"No seat called <b>{html.escape(label)}</b>.\n\n" + self._seat_list(),
                chat_id,
                thread_id,
            )
            return
        # No `selective`. That flag limits a forced reply to people named in the
        # text or the author of the message being replied to — and this question
        # names nobody and replies to nothing, so it opened the reply box for no
        # one at all.
        self._remember_handoff(seat.label, chat_id, thread_id, user)
        await self._say(
            ASK_FOR_TEXT.format(seat=html.escape(seat.label))
            + "\n\n<i>Reply to this, or just say it — either reaches "
            + html.escape(seat.label)
            + ".</i>",
            chat_id,
            thread_id,
        )

    # --- opening what is not running ----------------------------------------

    async def _open_application(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/open claude` — start an agent that is not running.

        No approval card. Opening an application changes nothing that has to be
        undone, and putting a card in front of it would make the fast thing slow
        for no safety bought — the gate is for what an agent does once it is
        open, which is exactly where the cards already are.
        """
        if not desktop.available():
            await self._say(
                "This machine cannot open applications — that is macOS only.", chat_id, thread_id
            )
            return

        catalogued = catalogue.known()
        if not typed:
            await self._offer_applications(catalogued, chat_id, thread_id)
            return

        app = catalogue.resolve(typed)
        if app is None:
            # Said, then asked. A name nobody knows is usually a name typed from
            # memory, and the useful reply is the list of real ones as buttons.
            await self._say(
                f"I do not know an application called <b>{html.escape(typed)}</b>.",
                chat_id,
                thread_id,
            )
            await self._offer_applications(catalogued, chat_id, thread_id)
            return

        where = await asyncio.to_thread(desktop.status, app)
        if not where.installed:
            await self._say(
                f"<b>{html.escape(app.name)}</b> is not installed on this machine.",
                chat_id,
                thread_id,
            )
            return
        if where.on_screen:
            await self._say(
                f"\u2705 <b>{html.escape(app.name)}</b> is already open.", chat_id, thread_id
            )
            return

        # Running but not on screen is the interesting third state, and reading
        # it as "already open" is what once reported an application that was
        # nowhere to be seen. An editor whose last window is closed keeps its
        # process, its helpers and its language server alive, so `is running`
        # says true and there is still nothing to look at. Opening it is exactly
        # what somebody wants here.
        waking = " It was running with no window." if where.running else ""

        if await asyncio.to_thread(desktop.open_, app):
            # "Asked to open", not "open". `open` returns once macOS has taken
            # the request, and a cold application takes seconds more to appear —
            # claiming otherwise would be a promise this cannot keep.
            await self._say(
                f"\U0001f680 Asked macOS to open <b>{html.escape(app.name)}</b>.{waking}",
                chat_id,
                thread_id,
            )
        else:
            await self._say(
                f"\U0001f6ab Could not open <b>{html.escape(app.name)}</b>.", chat_id, thread_id
            )

    async def _offer_applications(
        self, catalogued: list, chat_id: str, thread_id: int | None
    ) -> None:
        """Ask which one, with a button each.

        Because `/open` arrives bare. Telegram's own command menu pastes the
        command and stops, so the useful next thing is the question rather than
        a list somebody then has to type from — the same reason `/to` offers
        seats instead of printing them.

        Only what can actually be opened is offered: installed, and not already
        on screen. A button that answers "it is already open" is a button that
        wasted a tap.
        """
        standing = [(app, await asyncio.to_thread(desktop.status, app)) for app in catalogued]
        worth = [app.name for app, where in standing if where.installed and not where.on_screen]
        keyboard = cards.open_choices(tuple(worth))

        if keyboard is None:
            installed = [app for app, where in standing if where.installed]
            await self._say(
                "\u2705 Everything is already open."
                if installed
                else "Nothing openable is installed on this machine.",
                chat_id,
                thread_id,
            )
            return

        await self._say("Open which one?", chat_id, thread_id, reply_markup=keyboard)

    # --- saying what is wrong with this machine ------------------------------

    async def _report_health(self, chat_id: str, thread_id: int | None) -> None:
        """`/doctor` — the same check as at the desk, read from a phone.

        Run in-process and captured rather than shelled out, so it is the same
        code and cannot drift from what `halyard doctor` prints. Somebody was
        told to "check doctor" while away from the machine and could not; a
        check nobody can reach is a check nobody runs.
        """

        def look() -> tuple[str, int]:
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                try:
                    from halyard.doctor import run as check

                    problems = check()
                except Exception:
                    logger.warning("doctor failed", exc_info=True)
                    return ("", -1)
            return said.getvalue(), problems

        printed, problems = await asyncio.to_thread(look)
        if problems < 0:
            await self._say("\U0001f6ab The check itself failed. See the log.", chat_id, thread_id)
            return

        head = (
            "\u2705 <b>Nothing wrong here.</b>"
            if problems == 0
            else f"\u26a0\ufe0f <b>{problems} problem{'s' if problems != 1 else ''}.</b>"
        )
        await self._say(head, chat_id, thread_id)
        # Its own splitter, because this is a wall of aligned text and Telegram
        # takes 4096 bytes at a time. `<pre>` keeps the columns lined up.
        for chunk in cards.split_for_telegram(printed.rstrip()):
            await self._say(f"<pre>{html.escape(chunk)}</pre>", chat_id, thread_id)

    # --- labelling the task a branch is for ----------------------------------

    async def _reach_task(self, chat_id: str, thread_id: int | None):
        """The forge and the task this chat's branch is for, or None after
        having said what was missing.

        Every answer here is a configuration problem with a different fix, so
        each says which one it is rather than a single "cannot label".
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return None

        branch = await asyncio.to_thread(task_tracker.current_branch, found.path)
        number = task_tracker.number_of(branch or "")
        if number is None:
            await self._say(
                f"<code>{html.escape(branch or 'HEAD')}</code> is not named for a task, "
                "so there is nothing to label.",
                chat_id,
                thread_id,
            )
            return None

        origin = await asyncio.to_thread(task_tracker.origin_of, found.path)
        if origin is None:
            await self._say(
                "That repository has no <code>origin</code> to ask.", chat_id, thread_id
            )
            return None

        try:
            forge = task_tracker.build(origin, self._forge_token or "", declared=found.forge)
        except task_tracker.ForgeError as refused:
            await self._say(f"\U0001f6ab {html.escape(str(refused))}", chat_id, thread_id)
            return None
        return found, forge, number

    async def _label_task(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/label` — offer what could go on this branch's task, or add one."""
        reached = await self._reach_task(chat_id, thread_id)
        if reached is None:
            return
        project, forge, number = reached

        try:
            if typed:
                task = await task_tracker.put_on(forge, number, typed)
                await self._say(
                    f"\U0001f3f7 <b>{html.escape(typed.strip())}</b> added to "
                    f"#{task.number} \u2014 {html.escape(task.title)}",
                    chat_id,
                    thread_id,
                )
                return
            choice = await task_tracker.to_offer(forge, number, project.labels)
        except task_tracker.ForgeError as refused:
            await self._say(f"\U0001f6ab {html.escape(str(refused))}", chat_id, thread_id)
            return

        head = f"<b>#{choice.task.number}</b> \u2014 {html.escape(choice.task.title)}"
        if choice.task.labels:
            head += f"\n\nAlready: <code>{html.escape(', '.join(choice.task.labels))}</code>"
        if not choice.anything_left:
            await self._say(f"{head}\n\nNothing left to add.", chat_id, thread_id)
            return
        await self._say(
            f"{head}\n\nWhich label?",
            chat_id,
            thread_id,
            reply_markup=cards.label_choices(choice.offer),
        )

    # --- running a project's own commands -----------------------------------

    def _elapsed(self, seconds: float) -> str:
        """`3m 12s`. A bare count of seconds stops being readable at about 90."""
        if seconds < 90:
            return f"{seconds:.0f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"

    async def _run_command(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/command` — offer this project's commands, or start the named one.

        A command is a line or a list of them, run in order. Whatever follows
        the name answers the first value it asks for: `/command next-task 369`.
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        # A command started is a new question: one left open here is dropped,
        # so its answer cannot land on something else.
        self._inputs.pop((chat_id, thread_id), None)

        listed = commands_offered.offered(found.commands)
        names = (*(command.name for command in listed), *found.command_lists)
        if not names:
            await self._say(
                f"<b>{html.escape(found.name)}</b> lists no commands. Add a "
                "<code>commands:</code> block to it in <code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return

        if not typed:
            await self._say(
                "Run which one?",
                chat_id,
                thread_id,
                reply_markup=cards.command_choices(names),
            )
            return

        wanted, _, given = typed.strip().partition(" ")
        named = self._commands_named(found, wanted)
        if named is None:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no command called "
                f"<b>{html.escape(wanted)}</b>.",
                chat_id,
                thread_id,
            )
            await self._run_command("", chat_id, thread_id)
            return
        name, order = named

        asks = any(command_inputs.names_in(command.line) for command in order)
        if given.strip() and not asks:
            # Said rather than dropped: somebody who typed a value believes it
            # went somewhere.
            await self._say(
                f"<b>{html.escape(name)}</b> takes nothing after its name.", chat_id, thread_id
            )
            return
        if asks or any(command_labels.groups_in(command.line) for command in order):
            # Detached: a label is read from the task's tracker, and a value
            # may have to be asked for; this loop answers everybody meanwhile.
            self._detach(
                self._run_filled(found, name, order, chat_id, thread_id, given=given.strip()),
                "/command",
            )
            return
        await self._start_commands(found, name, order, chat_id, thread_id)

    def _commands_named(
        self, found: Project, wanted: str
    ) -> tuple[str, tuple[commands_offered.Command, ...]] | None:
        """A command by the name somebody typed or pressed, as what runs: one
        line, or the lines of a list in their order."""
        one = commands_offered.resolve(found.commands, wanted)
        if one is not None:
            return one.name, (one,)
        name = next(
            (key for key in found.command_lists if key.casefold() == wanted.casefold()), None
        )
        if name is None:
            return None
        return name, tuple(
            commands_offered.Command(name=entry, line=found.commands[entry])
            for entry in found.command_lists[name]
        )

    async def _run_filled(
        self,
        found: Project,
        name: str,
        order: tuple[commands_offered.Command, ...],
        chat_id: str,
        thread_id: int | None,
        *,
        given: str = "",
        typed: Mapping[str, str] | None = None,
    ) -> None:
        """Commands with something to fill in, filled — or what is missing asked for.

        A task's label first, then a typed value, and all of it before anything
        runs: a list stopping half way to wait for an answer would leave the
        project between two of its own commands.
        """
        lines = await self._command_lines(found, tuple(command.name for command in order))
        if lines.missing:
            await self._ask_for_label(
                found,
                lines,
                kind=cards.PICKED_FOR_COMMAND,
                name=name,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            return
        asks = tuple(dict.fromkeys(n for c in order for n in command_inputs.names_in(c.line)))
        values = dict(typed or {})
        if given and asks:
            values.setdefault(asks[0], given)
        if missing := [asked for asked in asks if asked not in values]:
            self._inputs[(chat_id, thread_id)] = (
                _Asked(project=found.name, command=name, name=missing[0], values=values),
                self._clock(),
            )
            await self._say(
                f"✍️ <b>{html.escape(name)}</b> takes <b>{html.escape(missing[0])}</b> "
                "— send it as your next message here.",
                chat_id,
                thread_id,
            )
            return
        filled = tuple(
            commands_offered.Command(
                name=command.name, line=command_inputs.filled(lines.lines[command.name], values)
            )
            for command in order
        )
        for before, after in zip(order, filled, strict=True):
            if command_inputs.names_in(before.line):
                logger.info("%s in %s runs as: %s", after.name, found.name, after.line)
        await self._start_commands(found, name, filled, chat_id, thread_id)

    async def _answer_input(self, chat_id: str, thread_id: int | None, text: str) -> bool:
        """Whether this message answered a value a command asked for here.

        Taken rather than read, as a hand-off to a seat is: it answers the next
        thing said and nothing after it, and not once the moment has passed.
        """
        held = self._inputs.pop((chat_id, thread_id), None)
        if held is None:
            return False
        asked, when = held
        if self._clock() - when > timedelta(seconds=HANDOFF_SECONDS):
            return False
        found = self._repositories.get(asked.project)
        named = self._commands_named(found, asked.command) if found else None
        if found is None or named is None:
            return False
        name, order = named
        self._detach(
            self._run_filled(
                found,
                name,
                order,
                chat_id,
                thread_id,
                typed={**asked.values, asked.name: text.strip()},
            ),
            "/command",
        )
        return True

    async def _start_commands(
        self,
        found: Project,
        name: str,
        order: tuple[commands_offered.Command, ...],
        chat_id: str,
        thread_id: int | None,
    ) -> None:
        """Start a command, or a list of them, unless another is running in the project."""
        # One at a time per project. Two `make` runs in one directory fight over
        # the same build outputs, and the second one's failure is a mystery.
        if busy := self._working.get(found.name):
            await self._say(
                f"\u23f3 <b>{html.escape(busy)}</b> is still running in "
                f"<b>{html.escape(found.name)}</b>. One at a time.",
                chat_id,
                thread_id,
            )
            return

        self._working[found.name] = name
        if len(order) == 1:
            said = f"\u25b6\ufe0f Running <code>{html.escape(order[0].line)}</code>\u2026"
        else:
            steps = " then ".join(html.escape(command.name) for command in order)
            said = f"\u25b6\ufe0f Running <b>{html.escape(name)}</b>: {steps}\u2026"
        await self._say(said, chat_id, thread_id)
        # Detached, because this can run for the better part of an hour and the
        # poller has approval cards to keep delivering while it does.
        task = asyncio.create_task(
            self._carry_out(found.name, found.path, name, order, chat_id, thread_id)
        )
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _carry_out(
        self,
        project: str,
        path: Path,
        name: str,
        order: tuple[commands_offered.Command, ...],
        chat_id: str,
        thread_id: int | None,
    ) -> None:
        """Run them to the end, one after another, saying how each went. The
        first that does not pass stops the rest, and says which did not run."""
        try:
            for place, command in enumerate(order):
                if len(order) > 1:
                    await self._say(
                        f"▶️ Running <code>{html.escape(command.line)}</code>…",
                        chat_id,
                        thread_id,
                    )
                result = await self._run_and_say(path, command, chat_id, thread_id)
                if result is not None and result.ok:
                    continue
                if rest := [later.name for later in order[place + 1 :]]:
                    await self._say(
                        f"⏹ <b>{html.escape(name)}</b> stopped at "
                        f"<b>{html.escape(command.name)}</b>, so "
                        f"{html.escape(', '.join(rest))} did not run.",
                        chat_id,
                        thread_id,
                    )
                return
        finally:
            # Released whatever happened. A project left marked busy by a crash
            # would refuse every command afterwards for no reason anybody could see.
            self._working.pop(project, None)

    async def _run_and_say(
        self, path: Path, command, chat_id: str, thread_id: int | None
    ) -> commands_running.Result | None:
        """Run one command to the end, and say what happened. None when it
        could not be started at all."""
        try:
            result = await asyncio.to_thread(commands_running.run, command.line, path)
        except Exception:
            logger.warning("A command from Telegram could not be run", exc_info=True)
            await self._say(
                f"\U0001f6ab <b>{html.escape(command.name)}</b> could not be started.",
                chat_id,
                thread_id,
            )
            return None

        if result.timed_out:
            head = f"\u23f1 <b>{html.escape(command.name)}</b> was stopped after "
        elif result.ok:
            head = f"\u2705 <b>{html.escape(command.name)}</b> finished in "
        else:
            head = f"\U0001f6ab <b>{html.escape(command.name)}</b> failed after "
        said = head + self._elapsed(result.seconds) + "."
        if result.output:
            said += f"\n\n<pre>{html.escape(result.output)}</pre>"
        await self._say(said, chat_id, thread_id)
        return result

    # --- committing what an agent wrote ------------------------------------
    #
    # Thin on purpose. Everything that decides anything — what git is asked,
    # what may be committed, whether a proposal is still answerable — lives in
    # `halyard.commits`. This resolves which repository a chat is about, moves
    # text between that package and Telegram, and nothing else.

    def _project_name_for(self, chat_id: str) -> str | None:
        """Which project a chat is about: its seat's, or the only one there is.

        A machine describing exactly one project answers for it from any chat,
        which is the single-seat setup that existed before seats were split
        across groups.
        """
        seat = for_chat(self._seats, chat_id) if chat_id else None
        if seat and seat.project:
            return seat.project
        if len(self._repositories) == 1:
            return next(iter(self._repositories))
        return None

    def _kept(self, chat_id: str, filename: str) -> Path | None:
        """Where a chat's kept state lives: under its project, the way
        `halyard.yaml` nests them — `projects/alpha-engine/last-said.json`.

        One file per project rather than one per machine: a project's agent
        prose stays with that project, its bound is its own, and removing a
        project leaves nothing of it mixed into another's. A chat no project
        owns keeps the machine-level file, where it always was. Beside the
        database either way, never inside the project's repository, where
        `/commit` would find it on every card.
        """
        return self._kept_for(self._project_name_for(chat_id), filename)

    def _kept_for(self, project: str | None, filename: str) -> Path | None:
        """The same place, for a project named outright rather than through a
        chat: a workflow takes its next step because a seat answered, and it
        knows which project that seat belongs to without a chat to ask about.
        """
        if self._said_path is None:
            return None
        if not project:
            return self._said_path.with_name(filename)
        # A directory named by configuration, so it must not be able to climb out.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", project).strip(".") or "_"
        return self._said_path.parent / "projects" / safe / filename

    async def _work_item(self, found: Project) -> str | None:
        """What rounds and runs are counted against — see `rounds.work_of`."""
        if found.path is None:
            return None
        return rounds.work_of(await asyncio.to_thread(current_branch, found.path), found.name)

    def _repository_for(self, chat_id: str) -> Project | None:
        """The project this chat is about, and where its code is.

        No configuration of its own: the chat already names a seat and a seat
        already names its project.
        """
        found = self._repositories.get(self._project_name_for(chat_id) or "")
        return found if found and found.path else None

    def _no_repository(self, chat_id: str) -> str:
        """Why `_repository_for` found nothing for this chat, said as the fix.

        The seat is named because it is the part nobody can see from the chat:
        a chat can belong to a seat nobody remembers setting up, and a message
        that only asks for a `path:` does not say whose.
        """
        head = "I do not know which repository this chat is about"
        seat = for_chat(self._seats, chat_id) if chat_id else None
        if seat is None:
            return (
                f"{head}: no seat has it. Give a seat "
                f"<code>chat: {html.escape(chat_id)}</code> in <code>halyard.yaml</code>."
            )
        label = html.escape(seat.label)
        if not seat.project:
            return (
                f"{head}: it is <b>{label}</b>'s, and {label} is under no project. "
                "Write it under its project in <code>halyard.yaml</code>."
            )
        return (
            f"{head}: it is <b>{label}</b>'s, and {label}'s project "
            f"<b>{html.escape(seat.project)}</b> has no <code>path:</code> in "
            "<code>halyard.yaml</code>."
        )

    def _message_runner(self, chat_id: str):
        """Whichever runtime this chat's seat uses, for the one-shot turn.

        The only place the commit flow touches a runtime at all, and it stays
        on this side of the boundary: `halyard.commits` is handed a finished
        sentence, never a way to ask for one.
        """
        seat = for_chat(self._seats, chat_id) if chat_id else None
        if seat and (found := self._runners.get(seat.runtime)):
            return found
        return self._runner

    async def _write_message(
        self, chat_id: str, work, inquiry: str = ""
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        """Have the work described: a subject line, and what actually changed.

        Both in one turn. The second half is the point of asking at all — the
        person deciding is away from the desk and has not seen this code, and a
        list of filenames says where an agent has been rather than what it did.

        Fails soft, on purpose. A model that cannot be reached should not cost
        somebody the commit — the reference computed from the branch is a
        usable message on its own, and `Rewrite` is one tap away.
        """
        runner = self._message_runner(chat_id)
        said = None
        if runner is not None and hasattr(runner, "ask"):
            try:
                said = await asyncio.wait_for(
                    runner.ask(
                        commits.prompt(work, inquiry),
                        model=MESSAGE_MODEL,
                        purpose="commit message",
                        project=self._project_name_for(chat_id),
                        system=commits.SYSTEM,
                    ),
                    timeout=MESSAGE_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning("Could not have a commit message written", exc_info=True)
        said = said or ""
        return (
            commits.assemble(work.reference, said) or None,
            commits.summary_of(said),
            commits.flag_of(said),
        )

    async def _propose_commit(
        self, chat_id: str, thread_id: int | None, *, full: bool = False
    ) -> None:
        """Read the branch's uncommitted work and offer to commit it.

        Two commands, one path. `/commit` proposes a message and stops there —
        which is what most changes want, and what a change to `scripts/` wants
        every time. `/review_and_commit` adds whatever the project has asked
        for: its own check, its warnings, and the round it offers instead of
        committing.

        Split by command rather than by a list of exceptions. A rule that says
        *these paths skip the gate* has to be maintained against a repository
        that keeps growing, and gets it wrong quietly; a second command is
        chosen at the moment somebody already knows which of the two they meant.
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        project, path = found.name, found.path

        # Off the event loop: git is a subprocess, and the poller has approval
        # cards to keep delivering while this reads a repository.
        reading = time.monotonic()
        work = await asyncio.to_thread(commits.read, path, project)
        logger.info(
            "commit %s: read the working tree in %.1fs", project, time.monotonic() - reading
        )
        if work.blocked:
            await self._say(f"\U0001f6ab {html.escape(work.blocked)}", chat_id, thread_id)
            return

        # Said before it starts, not after. A project's own check can run for
        # minutes, and silence for minutes reads as nothing having happened.
        checked = commits.Checked()
        if full:
            # `validate:` names one of the project's `commands:`, checked when
            # the file was read; what runs is the line written there.
            command = found.commands.get(found.validate) if found.validate else None
            skipping = commits.validation.only_documentation(work)
            if command and not skipping:
                await self._say(
                    f"\u23f3 Running <code>{html.escape(command)}</code>\u2026",
                    chat_id,
                    thread_id,
                )
            elif command:
                await self._say(
                    f"\U0001f4c4 Documentation only \u2014 not running "
                    f"<code>{html.escape(command)}</code>.",
                    chat_id,
                    thread_id,
                )

            # Progress from the worker thread, put back on the loop. Somebody
            # who pressed this is waiting for a card and cannot do anything else
            # with that thread, so a run that says nothing for three minutes
            # reads as Halyard having died rather than as a test suite working.
            loop = asyncio.get_running_loop()

            def progress(seconds: float, latest: str) -> None:
                asyncio.run_coroutine_threadsafe(
                    self._say(
                        f"\u2026 {self._elapsed(seconds)} \u2014 "
                        f"<code>{html.escape(latest[:150])}</code>",
                        chat_id,
                        thread_id,
                    ),
                    loop,
                )

            started = time.monotonic()
            checked = await asyncio.to_thread(
                partial(
                    commits.check,
                    work,
                    path,
                    command,
                    warn_if=found.warn_if,
                    on_progress=progress,
                )
            )
            logger.info(
                "commit %s: %d files, checks %s in %.1fs",
                project,
                len(work.changes),
                "skipped (documentation)" if checked.documentation_only else "ran",
                time.monotonic() - started,
            )
            if checked.refused:
                # No card at all. A failing check is a fact rather than a
                # judgement, so there is nothing here for somebody to weigh.
                said = (
                    f"\U0001f6ab <code>{html.escape(checked.refused)}</code> failed. "
                    "Nothing was committed."
                )
                if checked.output:
                    said += f"\n\n<pre>{html.escape(checked.output)}</pre>"
                await self._say(said, chat_id, thread_id)
                return

        asked = commits.confirmation.inquiry(found.confirmation, path) if full else ""
        writing = time.monotonic()
        message, summary, flag = await self._write_message(chat_id, work, asked)
        logger.info(
            "commit %s: message written in %.1fs (flag: %s)",
            project,
            time.monotonic() - writing,
            "yes" if flag else "no",
        )
        if not message:
            await self._say(
                "Could not write a commit message, and this branch is not named "
                "for an issue to fall back on. Commit this one at the desk.",
                chat_id,
                thread_id,
            )
            return

        # The model's flag rides with Halyard's own warnings. Both say the same
        # thing to a person — look here before tapping — and splitting them into
        # two blocks would make the card an argument between two authors.
        alarms = (*checked.warnings, *((flag,) if flag else ()))
        offers_round = full and commits.confirmation.offered(found.confirmation)
        handle = self._proposals.add(
            project, path, work, message, summary, alarms, reviewed=offers_round
        )
        await self._say(
            commit_card.render(
                project=project,
                work=work,
                message=message,
                summary=summary,
                warnings=alarms,
            ),
            chat_id,
            thread_id,
            reply_markup=commit_card.keyboard(handle, confirmation=offers_round),
        )

    async def _rewrite_commit(
        self, handle: str, text: str, chat_id: str, thread_id: int | None, user: str
    ) -> None:
        """Replace a proposed message with one somebody typed.

        Deliberately not a commit. The card comes back with the new wording and
        the same buttons, because the invariant worth keeping is that exactly
        one thing in this flow commits, and it is the Commit button — a typo
        typed on a phone should not be a commit nobody agreed to.
        """
        proposal = self._proposals.peek(handle)
        if proposal is None:
            await self._say("That commit is no longer open.", chat_id, thread_id)
            return
        written = commits.assemble(proposal.work.reference, text)
        if not written:
            await self._say("That message is empty.", chat_id, thread_id)
            return
        reworded = self._proposals.reword(handle, written)
        if reworded is None:
            await self._say("That commit is no longer open.", chat_id, thread_id)
            return
        await self._say(
            commit_card.render(
                project=reworded.project,
                work=reworded.work,
                message=reworded.message,
                summary=reworded.summary,
                warnings=reworded.warnings,
            ),
            chat_id,
            thread_id,
            reply_markup=commit_card.keyboard(handle, confirmation=reworded.reviewed),
        )

    async def _decide_commit(
        self, proposed: tuple[str, str], user_id: str, query_id: str, callback: dict
    ) -> None:
        """A button under a commit card."""
        handle, action = proposed
        # Checked exactly as an approval is. This one writes to a repository.
        if user_id not in self._authorized:
            await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
            logger.warning("Ignoring a commit button from unauthorized user %s", user_id)
            await self._dismiss(query_id)
            return

        message = callback.get("message") or {}
        here = str((message.get("chat") or {}).get("id") or "") or None
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")

        if action == commit_card.REWRITE:
            # Left in place: the sentence still has to find it.
            if handle not in self._proposals:
                await self._dismiss(query_id, "That commit is no longer open.")
                return
            await self._dismiss(query_id)
            await self._say(
                ASK_FOR_MESSAGE.format(handle=handle),
                here or "",
                thread_id,
                reply_markup={"force_reply": True},
            )
            return

        # Taken rather than read. That is what stops a second tap making a
        # second commit; there is no nonce to check.
        proposal = self._proposals.take(handle)
        if proposal is None:
            await self._dismiss(query_id, "That commit is no longer open.")
            return

        if action == commit_card.CONFIRM:
            await self._dismiss(query_id, "Sending the round…")
            self._detach(
                self._send_confirmation(proposal, here or "", thread_id, user_id, message_id),
                "confirmation round",
            )
            return

        if action == commit_card.DROP:
            await self._dismiss(query_id, "Cancelled.")
            await self._settle_commit(proposal, "\u2716\ufe0f CANCELLED", user_id, here, message_id)
            return

        try:
            sha = await asyncio.to_thread(commits.commit, proposal.path, proposal.message)
        except Exception as refused:
            logger.warning("A commit from Telegram failed", exc_info=True)
            await self._dismiss(query_id, "git refused.")
            await self._say(
                f"\U0001f6ab git refused: {html.escape(str(refused))}", here or "", thread_id
            )
            return

        # Said out loud, not only by editing the card. A toast disappears and an
        # edit two screens up is easy to scroll past; the one thing somebody
        # needs to leave with is that it happened, and what it is called.
        branch = proposal.work.branch
        done = [
            f"\u2705 <b>Committed</b> <code>{html.escape(sha)}</code> "
            f"on <code>{html.escape(branch)}</code>",
            "",
            f"<pre>{html.escape(proposal.message)}</pre>",
        ]
        outcome = f"\u2705 COMMITTED {sha}"

        if action == commit_card.SEND:
            await self._dismiss(query_id, f"Committed {sha}, pushing\u2026")
            try:
                where = await asyncio.to_thread(commits.push, proposal.path, branch)
            except Exception as refused:
                logger.warning("A push from Telegram failed", exc_info=True)
                # The commit is made and safe; only the push failed. Saying
                # which is the difference between "tap it again" and "somebody
                # else moved the branch, go to a desk".
                done += ["", f"\U0001f6ab but the push failed: {html.escape(str(refused))}"]
                outcome = f"\u2705 COMMITTED {sha} \u2014 not pushed"
            else:
                done[0] += f"\n\U0001f680 <b>Pushed</b> to <code>{html.escape(where)}</code>"
                outcome = f"\U0001f680 PUSHED {sha}"
        else:
            await self._dismiss(query_id, f"Committed {sha}")

        await self._say("\n".join(done), here or "", thread_id)
        await self._settle_commit(proposal, outcome, user_id, here, message_id)

    async def _send_confirmation(
        self,
        proposal,
        chat_id: str,
        thread_id: int | None,
        user_id: str,
        message_id: int | None,
    ) -> None:
        """Hand the project's round to its navigator, and commit nothing.

        The proposal is already taken by the time this runs, and stays taken.
        A round exists to change something — another look, a fix, another turn
        with the driver — so the card it came from describes a working tree that
        is about to be out of date. Committing afterwards is a fresh `/commit`,
        which reads the branch again.
        """
        found = self._repositories.get(proposal.project)
        round_text = commits.confirmation.review(
            found.confirmation if found else None, proposal.path
        )
        if not round_text:
            await self._say("That project has no confirmation round to send.", chat_id, thread_id)
            return

        seat = next(
            (
                one
                for one in self._seats
                if one.project == proposal.project and one.role is Role.NAVIGATOR
            ),
            None,
        )
        if seat is None:
            await self._say(
                f"<b>{html.escape(proposal.project)}</b> has no navigator to ask.",
                chat_id,
                thread_id,
            )
            return

        await self._settle_commit(
            proposal,
            f"\U0001f50d CONFIRMATION ROUND \u2014 sent to {seat.label}",
            user_id,
            chat_id,
            message_id,
        )
        # Through the same path a person uses, so the round lands in the
        # navigator's own conversation and is readable there afterwards.
        await self._forward_to_seat(
            f"{seat.label} {round_text}", f"tg:{user_id}", chat_id, thread_id
        )

    async def _settle_commit(
        self,
        proposal,
        outcome: str,
        user_id: str,
        chat_id: str | None,
        message_id: int | None,
    ) -> None:
        """Edit the card to say what happened, so scrolling back is honest."""
        if message_id is None:
            return
        try:
            await self._api.edit_message_text(
                chat_id or self._chat_id,
                message_id,
                commit_card.render_resolved(
                    project=proposal.project,
                    message=proposal.message,
                    outcome=outcome,
                    by=f"tg:{user_id}",
                ),
                reply_markup=None,
            )
        except Exception:
            logger.warning("Could not update a commit card", exc_info=True)

    async def _forward_last(
        self, label: str, actor: str, chat_id: str, thread_id: int | None
    ) -> None:
        """Hand the last thing said in this chat to another seat.

        The other half of `/to`, and the half somebody reaches for more often:
        an agent has just written a page and it needs to go to the seat that
        can act on it. Retyping it is not an option, and replying to it means
        finding it again above whatever has been said since.

        Nothing is remembered between the tap and the send. The message is
        already on disk — kept whole, before Telegram split it — so a bare
        `/forward` can offer the seats and the button can finish on its own.
        That is what `/to` cannot do, and why this is a separate command rather
        than a mode of that one.
        """
        kept = self._kept(chat_id, SAID_FILE)
        if kept is None:
            await self._say("Nothing here is keeping track of replies.", chat_id, thread_id)
            return

        said = last_said.last(kept, chat_id)
        if said is None:
            await self._say(
                "Nothing has been said in this chat yet, so there is nothing to hand on.",
                chat_id,
                thread_id,
            )
            return

        label = (label or "").strip()
        if not label:
            keyboard = cards.forward_choices(tuple(seat.label for seat in self._seats))
            if keyboard is None:
                await self._say(self._seat_list(), chat_id, thread_id)
                return
            when = _local(said.at).strftime("%H:%M")
            await self._say(
                f"Hand the reply from <b>{when}</b> to which seat?"
                f"\n\n<i>{html.escape(said.text[:200])}"
                f"{'…' if len(said.text) > 200 else ''}</i>",
                chat_id,
                thread_id,
                reply_markup=keyboard,
            )
            return

        if said.stale(self._clock()):
            # Said rather than refused: it is still what arrived, and somebody
            # who asked for it may well mean it. But a day-old reply handed to
            # an agent as though it were current is worth a sentence.
            await self._say(
                f"That reply is from {_local(said.at):%d %b %H:%M} — sending it anyway.",
                chat_id,
                thread_id,
            )

        logger.info("Forwarding to seat %s from %s: %r", label, actor, said.text[:60])
        await self._forward_to_seat(f"{label} {said.text}", actor, chat_id, thread_id)

    async def _run_inspection(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/inspect` — offer this project's inspections, or run the named one.

        The shape `/label` and `/command` have: a button per inspection, and the
        one pressed runs. It runs over the last reply in this chat, as a model
        turn of its own, with the inspection's text and what Halyard can see of the
        repository. A turn that could not be had is said as unmeasured rather
        than left out, because a missing answer reads exactly like a clean one.

        Whatever follows the name goes in as a note: `/inspect proof delivery`
        says which stage the reply belongs to, which is what most inspections
        ask first.
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        if not found.inspections:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no <code>inspections:</code> in "
                "<code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return
        kept = self._kept(chat_id, SAID_FILE)
        said = last_said.last(kept, chat_id) if kept else None
        if said is None:
            await self._say(
                "Nothing has been said in this chat yet, so there is nothing to inspect.",
                chat_id,
                thread_id,
            )
            return

        wanted, _, note = typed.strip().partition(" ")
        if not wanted:
            await self._say(
                f"Inspect the reply from <b>{_local(said.at):%H:%M}</b> with which one?",
                chat_id,
                thread_id,
                reply_markup=cards.inspection_choices(tuple(found.inspections)),
            )
            return
        name = next((key for key in found.inspections if key.casefold() == wanted.casefold()), None)
        if name is None:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no inspection called "
                f"<b>{html.escape(wanted)}</b>.",
                chat_id,
                thread_id,
            )
            await self._run_inspection("", chat_id, thread_id)
            return

        asker = self._inspector(chat_id, (chat_id, thread_id))
        if asker is None:
            await self._say("No runtime here can take a one-shot turn.", chat_id, thread_id)
            return

        path = found.inspections[name]
        arrived = _local(said.at).strftime("%H:%M")
        await self._say(
            f"\U0001f50e <b>{html.escape(name)}</b> is reading the last reply here "
            f"({arrived}, {len(said.text):,} characters) with "
            f"<code>{html.escape(str(path))}</code>…",
            chat_id,
            thread_id,
        )
        labels = await self._task_labels(found)
        known = await asyncio.to_thread(
            partial(
                frame.context,
                found.path,
                found.name,
                replied=arrived,
                reply=_files_of(said),
                labels=labels,
            )
        )
        chosen = found.inspection_models.get(name, ModelChoice()).over(self._inspection_model)
        answer = await inspecting.run(
            name,
            path,
            project=found.path,
            context=known,
            note=note.strip(),
            reply=said.text,
            asker=asker,
            model=chosen.model or INSPECTION_MODEL,
            effort=chosen.effort,
            timeout=INSPECTION_TIMEOUT_SECONDS,
            findings=found.label_findings,
            labeller=_Labelling(self, found),
            keeper=await self._keeper(found, asker.runtime),
            about=(
                f"reply {arrived}, {len(said.text)} chars, "
                f"from {said.agent_id or '?'} {said.session_id or '?'}"
            ),
        )
        if not answer.measured:
            await self._say(
                f"<b>{html.escape(name)}</b> · unmeasured — {html.escape(answer.why)}",
                chat_id,
                thread_id,
            )
            return
        findings = answer.text
        # Kept whole — what ran, on what, what it found, and the reply itself —
        # so a button under the answer hands a seat something it can read cold,
        # not just the piece of the answer the button sits under.
        if (results := self._kept(chat_id, RESULTS_FILE)) is not None:
            last_said.remember(
                results,
                chat_id=f"{chat_id}|{name}",
                text=inspecting.handed_on(
                    name,
                    path=path,
                    version=answer.version,
                    author=_seat_name(for_chat(self._seats, chat_id)) or "this chat",
                    arrived=arrived,
                    context=known,
                    findings=findings,
                    reply=said.text,
                ),
            )
        pieces = cards.split_for_telegram(findings)
        onward = cards.result_choices(name, tuple(seat.label for seat in self._seats))
        for index, chunk in enumerate(pieces):
            head = f"<b>{html.escape(name)}</b>\n" if index == 0 else ""
            await self._say(
                f"{head}<pre>{html.escape(chunk)}</pre>",
                chat_id,
                thread_id,
                reply_markup=onward if index == len(pieces) - 1 else None,
            )

    async def _send_result(
        self, value: str, actor: str, chat_id: str, thread_id: int | None
    ) -> None:
        """Hand an inspection's answer to a seat: the whole of it, as it was kept.

        Not the piece the button sits under — a long answer is split for
        Telegram — and with the line that says what it is, so the session it
        lands in can tell a finding from an instruction.
        """
        inspection, _, label = value.partition(">")
        results = self._kept(chat_id, RESULTS_FILE)
        kept = (
            last_said.last(results, f"{chat_id}|{inspection}")
            if results and inspection and label
            else None
        )
        if kept is None:
            await self._say(
                "That result is no longer kept here. Run the inspection again.",
                chat_id,
                thread_id,
            )
            return
        logger.info("Inspection %s result sent to %s by %s", inspection, label, actor)
        # Who it is for, in the words the configuration already has: the seat
        # and its role. The rest was written when the inspection answered.
        target = next((s for s in self._seats if s.label.casefold() == label.casefold()), None)
        greeting = f"To {_seat_name(target) or label}, from Halyard."
        await self._forward_to_seat(f"{label} {greeting}\n\n{kept.text}", actor, chat_id, thread_id)

    def _one_shot_runner(self, chat_id: str):
        """A runtime that can take a turn apart from any session, or None.

        This chat's own if it can, and the default one otherwise, so a report
        from any seat can be inspected — the channel's side of `inspections.Asker`.
        """
        runner = self._message_runner(chat_id)
        if not hasattr(runner, "ask"):
            runner = self._runner
        return runner if hasattr(runner, "ask") else None

    def _inspector(self, chat_id: str, destination: tuple[str, int | None]) -> _Inspecting | None:
        """This chat's one-shot runtime, for an inspection whose answer goes to
        `destination` — and so does anything the inspection asks to run."""
        runner = self._one_shot_runner(chat_id)
        if not runner:
            return None
        return _Inspecting(self, runner, destination, project=self._project_name_for(chat_id))

    async def _keeper(
        self, found: Project, runtime: str, counted: StepRounds | None = None
    ) -> _Keeping | None:
        """Where this project's inspection runs are kept, or None when they are
        not — `keep_inspections` off, or no database to keep them in."""
        if not self._keep_inspections or self._database is None:
            return None
        return _Keeping(
            self,
            project=found.name,
            work=await self._work_item(found),
            runtime=runtime,
            counted=counted,
        )

    def _destination_of(self, seat: Seat) -> tuple[str, int | None]:
        """Where a seat's traffic goes: its own chat, or the default one."""
        return (parse_destination(seat.chat) if seat.chat else None) or (self._chat_id, None)

    async def _stop_inspection(self, session: str, actor: str) -> None:
        """End an inspection's turn, closing any other card it still has open.

        Those are denied and closed before the turn is cancelled, while the
        inspection is still known and its cards can still say whose they were.
        """
        running = self._inspecting.get(session)
        if running is None:
            return
        running.stopped_by = actor
        for request, message_id, chat_id, _ in list(self._open.values()):
            if request.session_id != session:
                continue
            if await self._store.resolution_of(request.request_id) is not None:
                continue
            with contextlib.suppress(UnknownApprovalError):
                await self._store.deny(
                    request.request_id,
                    reason=ResolutionReason.USER,
                    note=f"Denied: {actor} stopped the inspection that asked for this.",
                )
            await self._settle_card(request, message_id, chat_id, "stop", actor)
        logger.info("Inspection %s stopped by %s", running.name, actor)
        running.turn.cancel()

    async def _reach_quietly(self, found: Project):
        """The forge and the task this project's branch is for, or None.

        `_reach_task` without the telling: nobody asked for what needs this, so
        a branch not named for a task, or a remote with no tracker behind it, is
        said only in the log.
        """
        if found.path is None:
            return None
        branch = await asyncio.to_thread(task_tracker.current_branch, found.path)
        number = task_tracker.number_of(branch or "")
        if number is None:
            return None
        origin = await asyncio.to_thread(task_tracker.origin_of, found.path)
        if origin is None:
            logger.info("%s#%d: the repository has no origin to ask", found.name, number)
            return None
        try:
            forge = task_tracker.build(origin, self._forge_token or "", declared=found.forge)
        except task_tracker.ForgeError as refused:
            logger.info("%s#%d: %s", found.name, number, refused)
            return None
        return forge, number

    async def _put_label(self, found: Project, label: str) -> None:
        """One label onto the task this project's branch is for, unless it is on
        it already. Logged whichever way it goes, never raised — see
        `_Labelling`."""
        reached = await self._reach_quietly(found)
        if reached is None:
            logger.info("%s went on no task: %s names none that can be reached", label, found.name)
            return
        forge, number = reached
        try:
            task = await asyncio.wait_for(forge.task(number), timeout=LABELS_TIMEOUT_SECONDS)
            if label.casefold() in {name.casefold() for name in task.labels}:
                logger.info("%s was already on %s#%d", label, found.name, number)
                return
            await asyncio.wait_for(
                task_tracker.put_on(forge, number, label), timeout=LABELS_TIMEOUT_SECONDS
            )
        except (task_tracker.ForgeError, TimeoutError) as refused:
            logger.warning(
                "Could not put %s on %s#%d: %s",
                label,
                found.name,
                number,
                str(refused) or "no answer in time",
            )
            return
        logger.info("Labelled %s#%d %s", found.name, number, label)

    async def _labels_on_task(self, found: Project) -> tuple[int | None, tuple[str, ...]]:
        """The task this project's branch is for, and the labels it carries.

        `(None, ())` for a branch naming no task that can be reached, and the
        number with no labels when the tracker would not say. Quiet, as
        `_task_labels` is: a command that needed a label asks for it anyway.
        """
        reached = await self._reach_quietly(found)
        if reached is None:
            return None, ()
        forge, number = reached
        try:
            task = await asyncio.wait_for(forge.task(number), timeout=LABELS_TIMEOUT_SECONDS)
        except (task_tracker.ForgeError, TimeoutError) as refused:
            logger.info(
                "No labels read from %s#%d for its commands: %s",
                found.name,
                number,
                str(refused) or "no answer in time",
            )
            return number, ()
        except Exception:
            logger.warning(
                "Could not read %s#%d's labels for its commands", found.name, number, exc_info=True
            )
            return number, ()
        return number, tuple(task.labels)

    async def _command_lines(self, found: Project, names: Sequence[str]) -> _Lines:
        """The lines these commands run as, each label they take filled in.

        From the task the branch is for, then from a label picked for this work
        since Halyard started — the task wins, being where a label is kept. A
        command taking none runs as written, and costs no call to the tracker.
        See `halyard.commands.labels`.
        """
        written = {name: found.commands.get(name, "") for name in names}
        wanted = {group for line in written.values() for group in command_labels.groups_in(line)}
        if not wanted:
            return _Lines(lines=written)
        task, carried = await self._labels_on_task(found)
        work = await self._work_item(found) or ""
        groups = {group: found.label_groups.get(group, ()) for group in wanted}
        values = {
            **self._picked.get((found.name, work), {}),
            **command_labels.values_from(groups, carried),
        }
        for name, line in written.items():
            if gaps := command_labels.missing(line, values):
                return _Lines(lines={}, missing=gaps[0], needing=name, task=task)
        filled = {name: command_labels.filled(line, values) for name, line in written.items()}
        for name in written:
            if filled[name] != written[name]:
                logger.info("%s for %s runs as: %s", name, work or found.name, filled[name])
        return _Lines(lines=filled, task=task)

    async def _ask_for_label(
        self,
        found: Project,
        lines: _Lines,
        *,
        kind: str,
        name: str,
        chat_id: str,
        thread_id: int | None,
        workflow: str | None = None,
        lead: str = "",
    ) -> None:
        """Offer a group's labels for a command the task gave no value.

        The tap puts the label on the task, so the next time — a second round,
        the other machine — nobody is asked; with no task to put it on, it is
        kept for this work until Halyard restarts.
        """
        group = lines.missing
        offered = found.label_groups.get(group, ())
        buttons = cards.label_picks(
            kind, name, list(found.label_groups).index(group), offered, workflow=workflow
        )
        if lines.task is not None:
            where = f"<b>{html.escape(found.name)}#{lines.task}</b> has none"
            after = "it goes on the task"
        else:
            where = "this branch names no task to read one from"
            after = "it is kept for this work until Halyard restarts"
        pick = f"Pick one — {after}." if buttons else "Put one on the task and press it again."
        await self._say(
            f"{lead}\U0001f3f7 <b>{html.escape(lines.needing)}</b> takes the task's "
            f"<b>{html.escape(group)}</b> label, and {where}. {pick}",
            chat_id,
            thread_id,
            reply_markup=buttons,
        )

    async def _label_picked(
        self, value: str, chat_id: str, thread_id: int | None, actor: str
    ) -> None:
        """A label tapped for a command: onto the task, kept for this work, and
        whatever needed it pressed again."""
        head, _, place = value.rpartition(">")
        head, _, group_place = head.rpartition(">")
        kind, name = head[:1], head[1:]
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        try:
            group = list(found.label_groups)[int(group_place)]
            label = found.label_groups[group][int(place)]
        except (ValueError, IndexError):
            await self._say(
                "That list has changed since it was offered — press it again.", chat_id, thread_id
            )
            return

        work = await self._work_item(found) or ""
        self._picked.setdefault((found.name, work), {})[group] = command_labels.value_of(label)
        logger.info("%s picked %s for %s in %s", actor, label, name, work or found.name)
        reached = await self._reach_quietly(found)
        if reached is not None:
            forge, number = reached
            task = f"<b>{html.escape(found.name)}#{number}</b>"
            try:
                await asyncio.wait_for(
                    task_tracker.put_on(forge, number, label), timeout=LABELS_TIMEOUT_SECONDS
                )
            except (task_tracker.ForgeError, TimeoutError) as refused:
                logger.warning("Could not put %s on %s#%d: %s", label, found.name, number, refused)
                await self._say(
                    f"\U0001f3f7 Could not put <b>{html.escape(label)}</b> on {task}: "
                    f"{html.escape(str(refused) or 'no answer in time')}. It is used for this "
                    "work until Halyard restarts.",
                    chat_id,
                    thread_id,
                )
            else:
                logger.info("Labelled %s#%d %s", found.name, number, label)
                await self._say(
                    f"\U0001f3f7 <b>{html.escape(label)}</b> is on {task}.", chat_id, thread_id
                )

        if kind == cards.PICKED_FOR_HANDOFF:
            await self._run_handoff(name, chat_id, thread_id, actor)
        elif kind == cards.PICKED_FOR_COMMAND:
            await self._run_command(name, chat_id, thread_id)
        elif kind == cards.PICKED_FOR_WORKFLOW:
            await self._resume_workflow("go", chat_id, thread_id)

    async def _task_labels(self, found: Project) -> dict[str, str]:
        """The task's labels from this project's own groups, for the envelope.

        Quiet where `/label` explains itself: nobody asked for these, so a
        branch not named for a task, a remote with no tracker behind it, or a
        tracker that does not answer adds nothing and says so only in the log.
        A project without `label_groups:` never asks the tracker at all.
        """
        if not found.label_groups:
            return {}
        reached = await self._reach_quietly(found)
        if reached is None:
            return {}
        forge, number = reached
        try:
            task = await asyncio.wait_for(forge.task(number), timeout=LABELS_TIMEOUT_SECONDS)
        except (task_tracker.ForgeError, TimeoutError) as missing:
            logger.info(
                "No task labels on the envelope for %s#%d: %s",
                found.name,
                number,
                str(missing) or "no answer in time",
            )
            return {}
        except Exception:
            logger.warning(
                "Could not read %s#%d's labels for the envelope", found.name, number, exc_info=True
            )
            return {}
        return task_tracker.picked(found.label_groups, task.labels)

    async def _files_at_reply(
        self, project: str | None, agent_id: str | None, session_id: str | None
    ) -> frame.Tree | None:
        """Where the project's files stood as a reply came in.

        Kept with the reply, so an inspection run on it later can say whether it is
        still looking at the code the reply was about. Bounded, because this is
        on the path that delivers the reply.
        """
        found = self._repositories.get(project or "")
        if found is None or found.path is None:
            return None
        try:
            files = await asyncio.wait_for(
                asyncio.to_thread(frame.tree, found.path), timeout=TREE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning(
                "Could not read %s's files in %.0fs; the reply goes on without them",
                project,
                TREE_TIMEOUT_SECONDS,
            )
            return None
        if files is not None:
            logger.info(
                "Reply from %s %s in %s: HEAD %s · Content %s",
                agent_id or "?",
                session_id or "?",
                project,
                files.head,
                files.content,
            )
        return files

    def _seats_for(self, project: str, to: str | None) -> list[Seat]:
        """Where a handoff can go: the seat `to:` names, the seats holding the
        role it names, or every seat of the project.

        A role held by two seats — a navigator on each runtime — offers both
        rather than guessing between them.
        """
        mine = [seat for seat in self._seats if seat.project == project] or list(self._seats)
        if not to:
            return mine
        named = [seat for seat in mine if seat.label.casefold() == to.casefold()]
        return named or [seat for seat in mine if seat.role and seat.role.value == to.casefold()]

    def _answer_since(self, last: flowing.Round, number: int) -> handing.Previous:
        """What the seat the last round went to has said since it got there.

        Read from what that seat's chat last heard, and only if it came after
        the round did: anything older is an answer to something else.
        """
        seat = find(self._seats, last.to)
        sent = _local(last.at).strftime("%H:%M")
        named = _seat_name(seat) or last.to
        where = parse_destination(seat.chat) if seat is not None else None
        chat = where[0] if where else self._chat_id
        kept = self._kept(chat, SAID_FILE)
        said = last_said.last(kept, chat) if kept is not None else None
        if said is None or said.at <= last.at:
            return handing.Previous(number=number - 1, seat=named, sent=sent)
        return handing.Previous(
            number=number - 1,
            seat=named,
            sent=sent,
            text=said.text,
            at=_local(said.at).strftime("%H:%M"),
        )

    async def _run_handoff(
        self,
        typed: str,
        chat_id: str,
        thread_id: int | None,
        actor: str,
        to: str | None = None,
    ) -> None:
        """`/handoff` — offer this project's handoffs, or make the named one.

        The shape `/inspect` has: a button per handoff, and the one pressed goes.
        It carries the last reply in this chat, the project's own text for the
        seat receiving it, and the answers of whichever inspections it names —
        run first, so they arrive together. Whatever follows the name goes in as
        a note, the way it does for an inspection.
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        if not found.handoffs:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no <code>handoffs:</code> in "
                "<code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return

        wanted, _, note = typed.strip().partition(" ")
        if not wanted:
            await self._say(
                "Hand the last reply here on how?",
                chat_id,
                thread_id,
                reply_markup=cards.handoff_choices(tuple(found.handoffs)),
            )
            return
        name = next((key for key in found.handoffs if key.casefold() == wanted.casefold()), None)
        if name is None:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no handoff called "
                f"<b>{html.escape(wanted)}</b>.",
                chat_id,
                thread_id,
            )
            await self._run_handoff("", chat_id, thread_id, actor)
            return
        handoff = found.handoffs[name]

        said = None
        if handoff.include_last_message:
            kept = self._kept(chat_id, SAID_FILE)
            said = last_said.last(kept, chat_id) if kept else None
            if said is None:
                await self._say(
                    "Nothing has been said in this chat yet, so there is nothing to hand on.",
                    chat_id,
                    thread_id,
                )
                return

        seats = self._seats_for(found.name, to or handoff.to)
        if len(seats) != 1:
            await self._say(
                f"Hand <b>{html.escape(name)}</b> to which seat?",
                chat_id,
                thread_id,
                reply_markup=cards.handoff_seat_choices(
                    name, tuple(seat.label for seat in seats or self._seats)
                ),
            )
            return
        [seat] = seats

        lines = await self._command_lines(found, handoff.commands)
        if lines.missing:
            await self._ask_for_label(
                found,
                lines,
                kind=cards.PICKED_FOR_HANDOFF,
                name=name,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            return
        await self._hand_off_once(
            found,
            name,
            handoff,
            seat,
            said,
            note=note.strip(),
            chat_id=chat_id,
            thread_id=thread_id,
            actor=actor,
            sender=_seat_name(for_chat(self._seats, chat_id)) or "this chat",
            command_lines=lines.lines,
        )

    async def _hand_off_once(
        self,
        found: Project,
        name: str,
        handoff: Handoff,
        seat: Seat,
        said: last_said.Said | None,
        *,
        note: str,
        chat_id: str,
        thread_id: int | None,
        actor: str,
        sender: str,
        extra: Sequence[str] = (),
        lead: str = "",
        buttons: dict | None = None,
        command_lines: Mapping[str, str] | None = None,
        counted: StepRounds | None = None,
    ) -> None:
        """Make one handoff: say so, run it, deliver it — and count the round
        when it is a workflow's step.

        What `/handoff` and a workflow's step have in common — everything above
        this chose *which* handoff goes where, and this is the making of it.
        `extra` are facts the caller adds to the envelope, `lead` goes in front
        of the line in the chat and `buttons` under it, which is how a step says
        which flow it belongs to and offers to stop it. `command_lines` are its
        commands as they run, with any task label in them — see `_command_lines`.
        `counted` is the step's rounds so far and how to count this one; a
        handoff pressed by hand has none, and says no round.
        """
        # One at a time per project, as for `/command`: two `make` runs in one
        # directory fight over the same outputs, and the second one's failure
        # is a mystery. Nothing is handed on; the handoff can be pressed again.
        if handoff.commands and (busy := self._working.get(found.name)):
            await self._say(
                f"⏳ <b>{html.escape(busy)}</b> is still running in "
                f"<b>{html.escape(found.name)}</b>, so <b>{html.escape(name)}</b> did "
                "not go. One at a time.",
                chat_id,
                thread_id,
            )
            return

        # Which round of its step this is, this one included — a workflow's
        # question only. Counted when it reaches the seat rather than here: a
        # step that went nowhere is the same round when it is sent again.
        done = counted.before if counted is not None else None
        number = len(done) + 1 if done is not None else None
        expected = counted.allowed if counted is not None else None
        previous = self._answer_since(done[-1], number) if done and number else None

        async def reached() -> None:
            """The round counts: the seat's session took the message."""
            if counted is None:
                return
            which = await counted.count(seat.label)
            logger.info(
                "Round %s of %s reached %s", rounds.shown(which, expected), name, seat.label
            )

        steps = [
            *([f"running {', '.join(handoff.commands)}"] if handoff.commands else []),
            *([f"inspecting {', '.join(handoff.inspections)}"] if handoff.inspections else []),
        ]
        first = f", {' then '.join(steps)} first" if steps else ""
        which = f" (round {rounds.shown(number, expected)})" if number and number > 1 else ""
        await self._say(
            f"{lead}\U0001f91d <b>{html.escape(name)}</b>{which} → "
            f"<b>{html.escape(_seat_name(seat))}</b>{html.escape(first)}…",
            chat_id,
            thread_id,
            reply_markup=buttons,
        )
        if handoff.commands:
            self._working[found.name] = f"handoff {name}"
        try:
            labels = await self._task_labels(found)
            known = await asyncio.to_thread(
                partial(
                    frame.context,
                    found.path,
                    found.name,
                    replied=_local(said.at).strftime("%H:%M") if said else "",
                    reply=_files_of(said),
                    labels=labels,
                )
            )
            inspector = (
                self._inspector(chat_id, self._destination_of(seat))
                if handoff.inspections
                else None
            )
            keeper = (
                await self._keeper(found, inspector.runtime, counted)
                if inspector is not None
                else None
            )
            handed = await handing.hand_off(
                handoff,
                project=found.path,
                context=[*known, *extra],
                note=note,
                reply=said.text if said else None,
                arrived=_local(said.at).strftime("%H:%M") if said else "",
                sender=sender,
                recipient_label=seat.label,
                recipient=_seat_name(seat),
                project_inspections=found.inspections,
                asker=inspector,
                model=self._inspection_model.model or INSPECTION_MODEL,
                effort=self._inspection_model.effort,
                models=found.inspection_models,
                timeout=INSPECTION_TIMEOUT_SECONDS,
                delivery=_SeatDelivery(
                    self, actor, chat_id, thread_id, accepted=reached if number else None
                ),
                findings=found.label_findings,
                labeller=_Labelling(self, found),
                project_commands={**found.commands, **(command_lines or {})},
                runner=_Running(self, found.path, chat_id, thread_id),
                round_number=number,
                expected=expected,
                previous=previous,
                keeper=keeper,
            )
        finally:
            # Released whatever happened, as `/command` does: a project left
            # marked busy would refuse every command after it.
            if handoff.commands:
                self._working.pop(found.name, None)
        outcomes = [
            *(f"<b>{html.escape(c.name)}</b>: {_ended(r)}" for c, r in handed.ran),
            *(
                f"<b>{html.escape(a.name)}</b>: {'answered' if a.measured else 'unmeasured'}"
                for a in handed.answers
            ),
        ]
        if outcomes:
            await self._say(" · ".join(outcomes), chat_id, thread_id)

    async def _run_workflow(
        self,
        typed: str,
        chat_id: str,
        thread_id: int | None,
        actor: str,
        *,
        start: int | None = None,
    ) -> None:
        """`/workflow` — take this project's handoffs in the order it wrote down.

        The shape `/handoff` has: a button per workflow, then a button per step
        of the one pressed, and it starts at the step pressed — the work is
        often past the first one already. `/workflow level3 review` starts
        there outright, with whatever follows as a note, and `/workflow level3
        applied phase 2` starts a step of the phases in the phase it names.
        With a run already going it says where that is instead — one working
        tree, one branch, one flow through it at a time.

        A run that stopped is not left behind by this: naming a step of the
        same workflow sends the stopped run on from there, its rounds and its
        phase kept, which is how somebody steers a run without taking the work
        out of it. Stopping it with the button is what starts afresh.
        """
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        flows = found.workflows.flows
        if not flows:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no <code>workflows:</code> in "
                "<code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return
        work = await self._work_item(found)
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        if work is None or kept is None:
            await self._say(
                "A run is counted against the work its branch names, and this checkout "
                "has no branch to name one.",
                chat_id,
                thread_id,
            )
            return
        run = await asyncio.to_thread(flowing.current, kept, work)

        wanted, _, rest = typed.strip().partition(" ")
        if not wanted:
            if run is not None:
                await self._say(
                    self._where_the_run_is(found, run),
                    chat_id,
                    thread_id,
                    reply_markup=cards.workflow_keyboard(run.workflow, go=not run.waiting),
                )
                return
            await self._say(
                "Take this project's handoffs how?",
                chat_id,
                thread_id,
                reply_markup=cards.workflow_choices(tuple(flows)),
            )
            return
        name = next((key for key in flows if key.casefold() == wanted.casefold()), None)
        if name is None:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no workflow called "
                f"<b>{html.escape(wanted)}</b>.",
                chat_id,
                thread_id,
            )
            await self._run_workflow("", chat_id, thread_id, actor)
            return
        if run is not None and run.waiting:
            await self._say(
                self._where_the_run_is(found, run),
                chat_id,
                thread_id,
                reply_markup=cards.workflow_keyboard(run.workflow),
            )
            return

        flow = flows[name]
        stretch = found.workflows.stretches.get(name)
        at, _, note = rest.strip().partition(" ")
        phase, note = _phase_named(note)
        if start is None and at:
            places = (i for i, step in enumerate(flow) if step.casefold() == at.casefold())
            start = next(places, None)
            if start is None:
                await self._say(
                    f"<b>{html.escape(name)}</b> has no step called <b>{html.escape(at)}</b>.",
                    chat_id,
                    thread_id,
                )
        if start is None or not 0 <= start < len(flow):
            await self._say(
                f"Start <b>{html.escape(name)}</b> at which step?",
                chat_id,
                thread_id,
                reply_markup=cards.workflow_steps(name, flow),
            )
            return
        inside = stretch is not None and stretch[0] <= start <= stretch[1]
        if phase is not None and not inside:
            await self._say(
                f"<b>{html.escape(flow[start])}</b> is not one of <b>{html.escape(name)}</b>'s "
                "phase steps, so it has no phase to start in.",
                chat_id,
                thread_id,
            )
            return

        continuing = run is not None and run.workflow == name
        if continuing:
            # Steered, not restarted: the rounds it has had and the phase it is
            # in stay, so a loop it was stopped in cannot be reset by a tap.
            assert run is not None
            going = run.at(
                start,
                phase=phase,
                entered=start if phase is not None or (inside and run.entered < 0) else None,
            )
        else:
            going = flowing.Run(
                workflow=name,
                step=start,
                since=self._clock(),
                chat=chat_id,
                thread=thread_id,
                waiting=False,
                by=actor,
                phase=phase or 1,
                entered=start if inside else -1,
            )
        kept_said = self._kept(chat_id, SAID_FILE)
        await self._take_step(
            found,
            work,
            going,
            last_said.last(kept_said, chat_id) if kept_said else None,
            _seat_name(for_chat(self._seats, chat_id)) or "this chat",
            note=note.strip(),
        )

    def _where_the_run_is(self, found: Project, run: flowing.Run) -> str:
        """One line about a run: for whoever asks, and for whoever it stopped in front of."""
        flow = found.workflows.flows.get(run.workflow) or ()
        step = flow[run.step] if run.step < len(flow) else "the end"
        where = (
            f"<b>{html.escape(run.workflow)}</b> {min(run.step + 1, len(flow))}/{len(flow)} · "
            f"{html.escape(step)}{self._phase_of(found, run)}"
        )
        if run.waiting:
            return f"▶️ {where} — waiting for <b>{html.escape(run.waiting_for)}</b>."
        return f"⏸ {where} — {html.escape(run.stopped or 'not started yet')}."

    async def _take_step(
        self,
        found: Project,
        work: str,
        run: flowing.Run,
        said: last_said.Said | None,
        sender: str,
        *,
        note: str = "",
    ) -> None:
        """Make the handoff the run is on, and wait for that seat's reply.

        The run is written down before the step goes, so a control plane that
        restarts in the middle comes back knowing what it was waiting for.
        """
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        flow = found.workflows.flows.get(run.workflow) or ()
        step = found.workflows.steps.get(flow[run.step]) if run.step < len(flow) else None
        handoff = found.handoffs.get(step.handoff) if step else None
        if kept is None or step is None or handoff is None:
            await self._stopped(found, work, run.held("its step is not one this project defines"))
            return
        seats = self._seats_for(found.name, step.seat or handoff.to)
        if len(seats) != 1:
            # A role two seats hold, and a step that did not say which. Said
            # rather than guessed: the wrong driver is a turn of somebody's work.
            await self._stopped(found, work, run.held(f"{step.name} names no one seat to go to"))
            return
        [seat] = seats

        lines = await self._command_lines(found, handoff.commands)
        if lines.missing:
            # A label is somebody's to pick, so the run waits here for it; the
            # tap puts it on the task and sends this step on.
            held = run.held(f"{lines.needing} takes a {lines.missing} label the task does not have")
            await asyncio.to_thread(flowing.save, kept, work, held)
            logger.info("Workflow %s for %s stopped: %s", run.workflow, work, held.stopped)
            await self._ask_for_label(
                found,
                lines,
                kind=cards.PICKED_FOR_WORKFLOW,
                name=run.workflow,
                chat_id=run.chat,
                thread_id=run.thread,
                workflow=run.workflow,
                lead=(
                    f"⏸ <b>{html.escape(run.workflow)}</b> {run.step + 1}/{len(flow)} · "
                    f"{html.escape(step.name)}{self._phase_of(found, run)} — "
                ),
            )
            return

        stretch = found.workflows.stretches.get(run.workflow)
        key = flowing.counted_as(step.name, run.step, phase=run.phase, stretch=stretch)
        # This step's own round is counted when it arrives, and what the
        # envelope says about the steps after it is said as of then.
        taken = run.taken()
        taken[key] = taken.get(key, 0) + 1
        when = f" at {_local(said.at).strftime('%H:%M')}" if said else ""
        extra = flowing.lines_for(
            run,
            flow=flow,
            steps=found.workflows.steps,
            taken=taken,
            seats=self._step_seats(found, flow),
            words=found.workflows.decisions,
            sent_back_by=f"{sender}{when}" if run.back else "",
            stretch=stretch,
            most=found.workflows.phases,
        )
        await asyncio.to_thread(flowing.save, kept, work, run.waiting_on(seat.label))
        logger.info(
            "Workflow %s step %d/%d (phase %d) for %s: %s → %s",
            run.workflow,
            run.step + 1,
            len(flow),
            run.phase,
            work,
            step.name,
            seat.label,
        )

        async def count(label: str) -> int:
            return await asyncio.to_thread(flowing.record, kept, work, key, to=label)

        await self._hand_off_once(
            found,
            step.handoff,
            handoff,
            seat,
            said,
            note=note,
            chat_id=run.chat,
            thread_id=run.thread,
            actor=run.by or "workflow",
            sender=sender,
            extra=extra,
            lead=(
                f"<b>{html.escape(run.workflow)}</b> {run.step + 1}/{len(flow)}"
                f"{self._phase_of(found, run)} · "
            ),
            buttons=cards.workflow_keyboard(run.workflow),
            command_lines=lines.lines,
            counted=StepRounds(
                before=run.rounds.get(key, ()),
                allowed=step.rounds,
                count=count,
                run=flowing.journal.run_id(run, work),
                step=step.name,
                phase=run.phase if "@" in key else None,
            ),
        )

    @staticmethod
    def _phase_of(found: Project, run: flowing.Run) -> str:
        """` · phase 2` for a run on a step of its flow's phases; nothing otherwise."""
        stretch = found.workflows.stretches.get(run.workflow)
        if stretch is None or not stretch[0] <= run.step <= stretch[1]:
            return ""
        return f" · phase {run.phase}"

    def _step_seats(self, found: Project, flow: Sequence[str]) -> dict[str, str]:
        """The seat each step of a flow goes to, by label, for the envelope to
        name where each decision would take the work. A step two seats could
        take is named by what it says — the role — since which one is not
        decided until it goes."""
        named: dict[str, str] = {}
        for name in flow:
            step = found.workflows.steps.get(name)
            handoff = found.handoffs.get(step.handoff) if step else None
            if step is None or handoff is None:
                continue
            seats = self._seats_for(found.name, step.seat or handoff.to)
            named[name] = seats[0].label if len(seats) == 1 else (step.seat or handoff.to or "")
        return named

    async def _stopped(
        self,
        found: Project,
        work: str,
        run: flowing.Run,
        *,
        go: bool = False,
        go_text: str = "",
        on: str = "",
    ) -> None:
        """Keep a run that is not going on, and say so where it was started.

        The card keeps the work in the run: send what is ready, leave the
        phases (`on` names where to), pick the step to go on from, or stop it.
        Taking it by hand would take it out of the run, and nothing asked for
        that.
        """
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        if kept is not None:
            await asyncio.to_thread(flowing.save, kept, work, run)
        logger.info("Workflow %s for %s stopped: %s", run.workflow, work, run.stopped)
        await self._say(
            self._where_the_run_is(found, run),
            run.chat,
            run.thread,
            reply_markup=cards.workflow_keyboard(
                run.workflow, go=go, go_text=go_text, on=on, pick=True
            ),
        )

    async def _finished(self, found: Project, work: str, run: flowing.Run, outcome: str) -> None:
        """The end of a run: what it did is kept for reading later, and one
        that went all the way says so in the chat it was started from.

        `outcome` is `done` or `stopped`. A stopped run is kept as well — how
        far work gets before somebody takes it over is as much a fact about
        it — but the chat is told that by the stop itself.
        """
        finished = self._clock()
        if self._database is not None:
            # Off to one side: the report below is what somebody is waiting for.
            self._detach(
                asyncio.to_thread(
                    partial(
                        flowing.journal.record,
                        self._database,
                        run,
                        project=found.name,
                        work=work,
                        outcome=outcome,
                        finished=finished,
                    )
                ),
                f"keep workflow {run.workflow}",
            )
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        if kept is not None:
            await asyncio.to_thread(flowing.clear, kept, work)
        logger.info("Workflow %s for %s %s", run.workflow, work, outcome)
        if outcome != "done":
            return
        flow = found.workflows.flows.get(run.workflow) or ()
        stretch = found.workflows.stretches.get(run.workflow)
        start, end = _local(run.since), _local(finished)
        shape = "%H:%M" if start.date() == end.date() else "%b %d %H:%M"
        await self._say(
            f"📋 <b>{html.escape(run.workflow)}</b> is done — {html.escape(work)}\n"
            f"{start.strftime(shape)} → {end.strftime(shape)} · "
            f"{flowing.journal.lasted(run.since, finished)}\n"
            f"{html.escape(flowing.journal.steps_line(run, flow=flow, stretch=stretch))}",
            run.chat,
            run.thread,
        )

    async def _advance_workflow(self, answered: Seat, text: str) -> None:
        """Take the next step, now that the seat a run was waiting for replied.

        Most replies are not a step: a seat nobody is waiting for costs a file
        read and nothing else. The decision is the reply's own last line, in
        the project's words for forward, back and wait — or, for a step that
        acts on the one before it, what that one decided. See
        `halyard.workflows`.
        """
        found = self._repositories.get(answered.project or "")
        if found is None or not found.workflows.flows:
            return
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        work = await self._work_item(found)
        if kept is None or work is None:
            return
        run = await asyncio.to_thread(flowing.current, kept, work)
        if run is None or not run.waiting or run.waiting_for != answered.label:
            return

        flow = found.workflows.flows.get(run.workflow) or ()
        stretch = found.workflows.stretches.get(run.workflow)
        decision, named = flowing.decided(text, found.workflows.decisions)
        moving = flowing.after(
            decision,
            run=run,
            flow=flow,
            steps=found.workflows.steps,
            taken=run.taken(),
            stretch=stretch,
            most=found.workflows.phases,
            named=named,
        )
        logger.info(
            "Workflow %s for %s: %s decided %s%s, acting on %s",
            run.workflow,
            work,
            answered.label,
            decision or "nothing",
            f" {named}" if named else "",
            moving.decided or "nothing",
        )
        if decision is None and moving.leaving is None:
            # Said rather than done quietly: a flow that moves on a reply
            # nobody wrote a decision into is a step somebody should see taken.
            here = found.workflows.steps.get(flow[run.step]) if run.step < len(flow) else None
            if moving.decided is not None and here is not None and here.decided_by:
                word = flowing.word_for(moving.decided, found.workflows.decisions)
                outcome = (
                    f"<b>{html.escape(here.decided_by)}</b>'s <b>{html.escape(word)}</b> stands"
                )
            else:
                outcome = f"<b>{html.escape(run.workflow)}</b> goes on"
            await self._say(
                f"<b>{html.escape(answered.label)}</b> ended with no decision line, so {outcome}.",
                run.chat,
                run.thread,
            )
        if moving.done:
            await self._finished(found, work, run, "done")
            return
        if moving.stop:
            target = moving.step
            waiting = moving.decided is flowing.Decision.WAIT
            if target is None and waiting and run.step + 1 < len(flow):
                # Waiting is for a person, and what they do next is the step
                # after this one — kept ready so the button sends it, with no
                # decision carried: the wait was the decision, and it is spent.
                target = run.step + 1
            parked = (
                run.at(
                    target,
                    back=moving.back,
                    carried=str(moving.carried or ""),
                    phase=moving.phase,
                    entered=moving.entered,
                )
                if target is not None
                else run
            )
            go_text = ""
            if target is not None and moving.phase is not None and moving.phase > run.phase:
                past = " anyway" if moving.phase > found.workflows.phases else ""
                go_text = f"↻ Phase {moving.phase} at {flow[target]}{past}"
            on = ""
            if moving.leaving is not None:
                on = flow[moving.leaving] if moving.leaving < len(flow) else "the end"
            await self._stopped(
                found,
                work,
                parked.held(
                    moving.stop, leaving=moving.leaving if moving.leaving is not None else -1
                ),
                go=target is not None,
                go_text=go_text,
                on=on,
            )
            return
        if moving.step is None:
            return
        if moving.phase is not None and moving.phase > run.phase:
            await self._say(
                self._phase_line(run.workflow, moving, flow, answered.label),
                run.chat,
                run.thread,
            )
        where = parse_destination(answered.chat) or (self._chat_id, None)
        said_kept = self._kept_for(found.name, SAID_FILE)
        await self._take_step(
            found,
            work,
            run.at(
                moving.step,
                back=moving.back,
                carried=str(moving.carried or ""),
                phase=moving.phase,
                entered=moving.entered,
            ),
            last_said.last(said_kept, where[0]) if said_kept else None,
            _seat_name(answered),
        )

    @staticmethod
    def _phase_line(workflow: str, moving: flowing.Next, flow: Sequence[str], by: str) -> str:
        """The chat's line for a phase starting: plain when it starts where
        phases start, and marked when a `next` named a step further on — the
        run leaving its usual path is something whoever reads this should see.
        """
        assert moving.step is not None and moving.phase is not None
        where = f"<b>{html.escape(flow[moving.step])}</b>"
        if not moving.skipped:
            return f"↻ <b>{html.escape(workflow)}</b> phase {moving.phase} starts at {where}."
        return (
            f"↪️ <b>{html.escape(workflow)}</b> phase {moving.phase} starts at {where}, "
            f"skipping {html.escape(', '.join(moving.skipped))} — "
            f"<b>{html.escape(by)}</b> named where to start."
        )

    async def _resume_workflow(self, action: str, chat_id: str, thread_id: int | None) -> None:
        """The buttons on a run: send the step it stopped before (`go`), leave
        its phases instead (`on`), offer its steps to go on from (`pick`), or
        stop it (`stop`)."""
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(self._no_repository(chat_id), chat_id, thread_id)
            return
        kept = self._kept_for(found.name, WORKFLOW_FILE)
        work = await self._work_item(found)
        run = await asyncio.to_thread(flowing.current, kept, work) if kept and work else None
        if run is None or kept is None or work is None:
            await self._say("No workflow is going here.", chat_id, thread_id)
            return
        if action == "stop":
            await self._finished(found, work, run, "stopped")
            await self._say(
                f"⏹ <b>{html.escape(run.workflow)}</b> stopped at step {run.step + 1}. "
                "Its handoffs can still be pressed by hand.",
                chat_id,
                thread_id,
            )
            return
        if run.waiting:
            await self._say(self._where_the_run_is(found, run), chat_id, thread_id)
            return
        flow = found.workflows.flows.get(run.workflow) or ()
        if action == "pick":
            await self._say(
                f"Send <b>{html.escape(run.workflow)}</b> on from which step"
                f"{self._phase_of(found, run).replace(' · ', ', in ')}? Its rounds so far "
                "are kept.",
                chat_id,
                thread_id,
                reply_markup=cards.workflow_steps(run.workflow, flow),
            )
            return
        if action == "on":
            # Only from a stop at the end of a phase, which is the one that
            # says where leaving goes. The run is parked at the next phase, so
            # the phase it leaves in is the one before that. A button left on
            # an older card finds nothing to leave: the run has moved since.
            stretch = found.workflows.stretches.get(run.workflow)
            if stretch is None or run.leaving < 0:
                await self._say(self._where_the_run_is(found, run), chat_id, thread_id)
                return
            if run.leaving >= len(flow):
                await self._finished(found, work, run, "done")
                return
            run = run.at(run.leaving, phase=max(run.phase - 1, 1), entered=stretch[0])
        source = find(self._seats, run.waiting_for) if run.waiting_for else None
        where = (parse_destination(source.chat) if source and source.chat else None) or (
            run.chat,
            run.thread,
        )
        said_kept = self._kept_for(found.name, SAID_FILE)
        await self._take_step(
            found,
            work,
            run,
            last_said.last(said_kept, where[0]) if said_kept else None,
            _seat_name(source) or _seat_name(for_chat(self._seats, run.chat)) or "this chat",
        )

    async def _offer_seats(
        self, text: str, chat_id: str, thread_id: int | None, anchor_id: int | None
    ) -> None:
        """Ask which seat, attached to the message that holds the text.

        Sent as a reply on purpose. Pressing a button then reads the exact
        message back out of the callback rather than out of the preview above
        it, which is shortened for reading — and a button that sends a
        *truncated* version of what it is shown next to would be worse than no
        button.
        """
        keyboard = cards.seat_choices(tuple(seat.label for seat in self._seats))
        if keyboard is None:
            await self._say(self._seat_list(), chat_id, thread_id)
            return
        preview = text if len(text) <= 200 else text[:200] + "…"
        await self._say(
            f"Send this to which seat?\n\n<blockquote>{html.escape(preview)}</blockquote>",
            chat_id,
            thread_id,
            reply_markup=keyboard,
            reply_to_message_id=anchor_id,
        )

    async def _forward_to_session(
        self,
        text: str,
        actor: str,
        chat_id: str,
        thread_id: int | None = None,
        accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Put a typed message into the session that chat belongs to.

        Started as a detached task rather than awaited. A turn runs tools, and
        each tool may stop for an approval — which arrives as a button press
        this same poll loop has to read. Waiting here for the turn to finish
        would mean waiting for an approval that can never be delivered.
        """
        if self._gate.paused:
            await self._say("⏸ Paused. Send /resume first.", chat_id, thread_id)
            return
        if not (self._runner or self._runners) or self._registry is None:
            await self._say(
                "This control plane cannot send messages into a session. That needs "
                "the agent CLI, so it has to run on the host rather than in a container.",
                chat_id,
                thread_id,
            )
            return

        found = await self._session_for(chat_id)
        if found is not None and found.runner.busy(found.session_id):
            # The runner serialises per session, so this would sit in silence
            # until the turn before it finished. Silence is what makes people
            # think a message was lost.
            await self._say(
                "⏳ Still working on the last one — yours is queued behind it.",
                chat_id,
                thread_id,
            )
        if found is None:
            # Name the chat. Without it this says a seat is missing without
            # saying which one to add, and the id it wants is the one piece of
            # information nobody can look up from where they are standing — it
            # is not shown anywhere in Telegram's own interface.
            seat = self._seat_for_chat(chat_id)
            why = await self._why_unreachable(seat) if seat is not None else None
            if seat is None:
                said = (
                    f"No seat owns this chat (<code>{chat_id}</code>). Add it to a "
                    "seat's <code>chat:</code> in your seat configuration, then "
                    "restart — seats are read at startup."
                )
            elif why:
                # The runtime could not be asked at all, which is a different
                # sentence from "it has no such session". Measured: an opencode
                # TUI started without `--port` answers on no port, the lookup
                # comes back empty, and this said the seat's session did not
                # exist — while it sat open on the screen under that exact name.
                said = (
                    f"The <b>{html.escape(seat.label)}</b> seat's session could not "
                    f"be looked up — {html.escape(seat.runtime)} did not answer."
                    f"\n\n<pre>{html.escape(why)}</pre>"
                )
            else:
                said = (
                    f"The <b>{seat.label}</b> seat owns this chat, but "
                    f"{seat.runtime} has no session named "
                    f"<code>{seat.session}</code>. Check it with "
                    "<code>halyard doctor</code>."
                )
            await self._say(said, chat_id, thread_id)
            return

        task = asyncio.create_task(self._deliver(found, text, actor, chat_id, thread_id, accepted))
        # Held so the loop does not drop the only reference and cancel it.
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _session_for(self, chat_id: str) -> _SessionTarget | None:
        """Which runtime-owned session a chat belongs to.

        The configured name is tried first. It is addressable from a standing
        start — a control plane that restarted a second ago can still find the
        session — whereas the registry only knows what has fired a hook since it
        came up. Telling somebody to go run a command somewhere before they can
        send a message is not an answer.
        """
        # The seat that owns this group, if one does. That is the whole
        # routing rule in this direction: a group is a seat, a seat knows its
        # runtime and its session, and nothing has to be worked out per message.
        seat = self._seat_for_chat(chat_id)
        role = seat.role if seat else self._role_for_chat(chat_id)

        name = seat.session if seat else (role and self._session_names.get(role))
        if name:
            found = await self._resolve(seat, name)
            if found:
                runner = self._runner_for(seat)
                if runner is not None:
                    return _SessionTarget(found.session_id, self._project, found.cwd, runner)
            logger.warning(
                "No session named %r for the %s seat", name, seat.label if seat else role
            )

        # A chat no seat owns, which is a real way to work: sessions started for
        # small jobs report into the bot's own chat, several of them, and
        # answering one there is the natural thing to do.
        #
        # Answering it must reach the session whose message is being answered.
        # The fallback below takes whichever session was heard from last, which
        # is usually the same one and silently is not: a second session running
        # a command in between moves "last" without anybody seeing it, and the
        # reply lands in a conversation nobody was reading.
        #
        # What was delivered here is already written down, with the runtime that
        # owns the id — a session id means nothing without one.
        kept = self._kept(chat_id, SAID_FILE)
        if seat is None and role is None and kept is not None:
            spoke = last_said.last(kept, chat_id)
            if spoke is not None and spoke.session_id and spoke.agent_id:
                runner = self._runners.get(spoke.agent_id)
                if runner is not None:
                    logger.info(
                        "Answering %s in %s, which is what last spoke there",
                        spoke.session_id,
                        chat_id,
                    )
                    return _SessionTarget(spoke.session_id, self._project, None, runner)

        session = (
            await self._registry.latest_for_role(role)
            if role is not None
            else await self._registry.latest()
        )
        if session is None:
            return None
        runner = self._runners.get(session.agent_id) or self._runner_for(seat)
        if runner is None:
            return None
        return _SessionTarget(session.session_id, session.project, session.cwd, runner)

    def _seat_for_chat(self, chat_id: str) -> Seat | None:
        return for_chat(self._seats, chat_id) if self._seats else None

    def _runner_for(self, seat: Seat | None):
        """The runtime a seat is, falling back to the only one there is."""
        return (seat and self._runners.get(seat.runtime)) or self._runner

    async def _resolve(self, seat: Seat | None, name: str):
        """Ask that seat's runtime what the name means.

        The channel used to import Claude Code's lookup directly, which made a
        second runtime impossible to add without editing this file — and would
        have gone looking for a Codex thread in `~/.claude`.
        """
        runner = self._runner_for(seat)
        if runner is None:
            return None
        return await asyncio.to_thread(runner.resolve, name)

    async def _why_unreachable(self, seat: Seat) -> str | None:
        """Why a seat's runtime could not be asked, if that is what happened.

        Asked of the runtime itself, through the check `halyard doctor` runs
        first — so this channel learns the reason without knowing a thing about
        the runtime. Only a failure is carried. A runtime that answered and has
        no session by that name is the other message, and that one is true.
        """
        from halyard.agents import registry

        spec = registry.get(seat.runtime)
        if spec is None or spec.check_available is None:
            return None
        try:
            found = await asyncio.to_thread(
                spec.check_available, **self._check_contexts.get(seat.runtime, {})
            )
        except Exception:
            logger.exception("Could not ask %s why it did not answer", seat.runtime)
            return None
        # A failure and the lines that belong to it — the fix is usually in the
        # continuation, and "nothing is answering" without "start it with
        # --port" is half an answer.
        said: list[str] = []
        for level, text in found:
            if level == "fail" or (not level and said):
                said.append(text)
            elif said:
                break
        return "\n".join(said) or None

    def _role_for_chat(self, chat_id: str) -> Role | None:
        for role, destination in self._routes.items():
            if destination and destination[0] == chat_id:
                return role
        return None

    async def _deliver(
        self,
        session: _SessionTarget,
        text: str,
        actor: str,
        chat_id: str | None = None,
        thread_id: int | None = None,
        accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        session_id, project, cwd = session.session_id, session.project, session.cwd
        runtime = getattr(session.runner, "id", "?")

        async def stopped_afterwards(reason: str) -> None:
            """Say so when a turn that *was* accepted then fell over.

            A different sentence from the one below, because it is a different
            thing: the message arrived, work happened, and then it stopped. The
            two were one message before, and it read "that did not reach" for a
            turn that had been running for a quarter of an hour.
            """
            await self._say(
                f"⚠️ The turn in <b>{html.escape(str(session_id))}</b> "
                f"({html.escape(str(runtime))}) stopped."
                f"\n\n<pre>{html.escape(the_useful_end(reason))}</pre>",
                chat_id,
                thread_id,
            )

        delivered = False
        try:
            delivered = await session.runner.send(session_id, text, cwd, stopped_afterwards)
        except Exception:
            logger.exception("Could not deliver a message to %s", session_id)
        finally:
            await self._record(
                user_message(
                    session_id=session_id,
                    actor=actor,
                    project=project,
                    length=len(text),
                    delivered=delivered,
                )
            )
        if delivered and accepted is not None:
            # Whatever was waiting for it to land — a handoff's round, counted
            # only now, so a message that reached nobody is not one. Failing
            # here costs that and nothing else: the message is in.
            try:
                await accepted()
            except Exception:
                logger.exception("Could not note that a message reached %s", session_id)
        if not delivered:
            # Name what was tried. "Check the log" is the message this project
            # keeps having to replace: the person reading it is on a phone,
            # away from the machine, and the one fact they cannot recover from
            # there is which session this went to and under which runtime.
            # Two runtimes can hold one name, so neither half is enough alone.
            #
            # This now means what it says. It used to also cover a turn that
            # had been accepted and was killed by a timeout fifteen minutes
            # later; that case goes through `stopped_afterwards` above.
            because = getattr(session.runner, "last_error", lambda _: None)(session_id)
            # The runtime usually said why, on a stream this used to discard.
            # "Not logged in · Please run /login" was printed by the CLI, thrown
            # away, and replaced with an instruction to read a log on a machine
            # the person had walked away from.
            detail = (
                f"\n\n<pre>{html.escape(the_useful_end(because))}</pre>"
                if because
                else "\n\nNothing was printed. <code>halyard doctor</code> checks the rest."
            )
            await self._say(
                f"⚠️ That did not reach <b>{html.escape(str(session_id))}</b> "
                f"({html.escape(str(runtime))}).{detail}",
                chat_id,
                thread_id,
            )

    async def _status(self) -> str:
        state = await self._gate.state()
        open_requests = await self._store.list_open()
        lines = [
            f"<b>Halyard — {html.escape(self._project)}</b>",
            "",
            f"Gate: {'⏸ paused' if state.paused else '▶️ running'}",
        ]
        if state.changed_by:
            lines.append(f"  last changed by {html.escape(state.changed_by)}")

        seats = await self._describe_seats()
        if seats:
            lines += ["", "<b>Sessions</b>"]
            lines += seats

        lines.append("")
        lines.append(f"Open approvals: {len(open_requests)}")
        for request in open_requests[:5]:
            remaining = cards.format_remaining(request.expires_at, self._clock())
            lines.append(
                f"  • {html.escape(request.project)} — "
                f"<code>{html.escape(request.command_summary[:60])}</code> ({remaining})"
            )
        return "\n".join(lines)

    async def _choose(
        self, what: str, value: str, chat_id: str | None, thread_id: int | None
    ) -> None:
        """Show or set the model or effort a seat will use.

        Only for turns started from here. Without an override, a resumed Claude
        session inherits its existing model. Other runtimes answer through the
        same preference interface with their own defaults.
        """
        seat = self._seat_for_chat(chat_id or "")
        runner = self._runner_for(seat)
        if runner is None:
            await self._say("No runner: this control plane cannot start turns.", chat_id, thread_id)
            return

        found = await self._session_for(chat_id or "")
        if found is None:
            await self._say("No session for this chat.", chat_id, thread_id)
            return
        session_id = found.session_id
        # Use the runner carried by the resolved target. The registry fallback
        # may know the runtime even when the chat has no configured seat.
        runner = found.runner

        # Ask the runtime what it accepts rather than importing one runtime's
        # list. The channel held `EFFORT_LEVELS` from the Claude Code module
        # until a Codex investigation pointed at it: a chat layer that knows a
        # specific runtime's constants is the thing this architecture exists to
        # prevent, and it would have rejected a perfectly valid Codex effort.
        #
        # `enforced` is why the flag is in `options()` at all. Effort is a
        # closed set worth checking; models are not, and refusing one released
        # this morning because it is missing from a list written months ago
        # would be worse than passing it through.
        offered = runner.options(session_id)
        if what not in offered:
            # A runtime that does not have this setting at all. Left to fall
            # through, `/effort high` on an opencode seat answered "turns from
            # here will use high" and set nothing, because its messages carry a
            # model and nothing about how hard it thinks. A confirmation for
            # something that did not happen is worse than a refusal.
            await self._say(
                f"{seat.runtime if seat else 'This runtime'} has no <b>{what}</b> setting"
                + (
                    f" — it takes {', '.join(sorted(offered))}."
                    if offered
                    else ", and nothing here can be chosen."
                ),
                chat_id,
                thread_id,
            )
            return
        allowed, enforced = offered.get(what, ((), False))
        if value and enforced and value.lower() not in allowed:
            await self._say(
                f"{what.capitalize()} is one of: <code>{' '.join(allowed)}</code>",
                chat_id,
                thread_id,
            )
            return

        if value:
            setter = runner.set_model if what == "model" else runner.set_effort
            cleared = value.lower() in ("default", "clear", "reset")
            setter(session_id, None if cleared else value)
            if cleared:
                model, effort = runner.preferences(session_id)
                back_to = model if what == "model" else effort
                answer = (
                    f"Cleared. Turns from here will use <b>{html.escape(back_to)}</b>."
                    if back_to
                    else (
                        f"Cleared. Turns from here will leave the {what} "
                        "to the resumed session/runtime."
                    )
                )
            else:
                answer = f"Turns started from here will use <b>{html.escape(value)}</b>."
            await self._say(answer, chat_id, thread_id)
            return

        model, effort = runner.preferences(session_id)
        chosen = model if what == "model" else effort
        role = seat.role if seat else self._role_for_chat(chat_id or "")
        wanted = seat.session if seat else self._session_names.get(role or Role.NAVIGATOR, "")
        ref = await self._resolve(seat, wanted or "")
        in_use = (ref.model if what == "model" else ref.effort) if ref else None
        lines = [f"<b>{what}</b>", f"  in the session: {html.escape(str(in_use or 'unknown'))}"]
        if chosen:
            lines.append(f"  from here: <b>{html.escape(chosen)}</b>")
        lines.append(f"\nSet with <code>/{what} &lt;value&gt;</code>, or <code>default</code>.")
        # Buttons when the runtime named a closed set, and the typed form
        # regardless: models are open-ended, so a name released this morning
        # has to work whether or not it is on a keyboard written months ago.
        await self._say(
            "\n".join(lines), chat_id, thread_id, reply_markup=cards.choices(what, allowed)
        )

    def _options(self, chat_id: str | None = None) -> str:
        """Everything that can be chosen, asked of the runtime rather than known.

        One message, because the question it answers — "what can I even say
        here?" — is asked from a phone, where reading a manual is not an option
        and a wrong guess costs a round trip.

        Nothing here is hardcoded in this module. A second runtime shows up in
        this output by existing, and a model list updated in the environment
        appears without a release.
        """
        runner = self._runner_for(self._seat_for_chat(chat_id or ""))
        if runner is None:
            return "No runner: this control plane cannot start turns."

        lines = [f"<b>{html.escape(runner.id)}</b>"]
        for name, (values, enforced) in runner.options().items():
            shown = " ".join(html.escape(v) for v in values)
            lines.append(f"\n/{name}  <code>{shown}</code>")
            if not enforced:
                # Otherwise a model released after this list was written looks
                # unavailable, and the honest answer is that it probably works.
                lines.append("  ↳ anything else is passed through and may work.")
        lines.append("\nAdd <code>default</code> to give a choice back to the session.")
        return "\n".join(lines)

    async def _describe_seats(self) -> list[str]:
        """One line per configured seat: what it is, and what is answering.

        Which model a seat is on is invisible from a phone otherwise, and in a
        navigator/driver pair the two are usually deliberately different — a
        thinking one and a cheap one. Worth being able to check before sending
        an expensive instruction to the wrong one.
        """
        # Every seat, not every role: there are two drivers now, and telling
        # them apart is the point of listing them at all.
        configured = self._seats or [
            Seat(label=role.value, runtime=default_runtime(), session=name, role=role)
            for role, name in self._session_names.items()
        ]

        lines: list[str] = []
        for seat in configured:
            name = seat.session
            if not name:
                continue
            ref = await self._resolve(seat, name)
            label = f"{seat.label} ({seat.runtime}): <b>{html.escape(name)}</b>"
            if ref is None:
                lines.append(f"  {label} — not found")
                continue
            details = " · ".join(filter(None, [ref.model, ref.effort and f"effort {ref.effort}"]))
            seat_runner = self._runner_for(seat)
            busy = " · ⏳ working" if seat_runner and seat_runner.busy(ref.session_id) else ""
            lines.append(f"  {label}\n     at the desk: {html.escape(details) or 'unknown'}{busy}")
            # Where this seat stands against its usage windows, when its runtime
            # keeps that. Asked of the registry, which asks the runtime — this
            # module has never known what a rate limit looks like and should not
            # start now.
            if standing := transcripts.usage_for(ref.session_id, watching_for(seat.runtime)):
                lines.append(f"     used: {html.escape(' · '.join(standing))}")
            if seat_runner is not None:
                model, effort = seat_runner.preferences(ref.session_id)
                mine = " · ".join(filter(None, [model, effort and f"effort {effort}"]))
                lines.append(
                    f"     from here: {html.escape(mine) or 'inherits the session/runtime'}"
                )
        return lines

    async def _say(
        self,
        text: str,
        chat_id: str | None = None,
        thread_id: int | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Answer in the conversation that asked.

        Not in the configured default chat, which is where this used to go: ask
        the navigator group something and the reply appeared in a private chat
        with the bot, which reads as the message having been lost.
        """
        try:
            await self._api.send_message(
                chat_id or self._chat_id,
                text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            logger.warning("Could not answer a command", exc_info=True)

    async def _handle_callback(self, callback: dict) -> None:
        query_id = str(callback.get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))

        chosen = cards.parse_choice_data(callback.get("data") or "")
        if chosen is not None:
            # Checked exactly as an approval is. Setting the model for a seat
            # is not an approval, but it is still an action taken on somebody
            # else's session, and the button is visible to a whole group.
            if user_id not in self._authorized:
                await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
                await self._dismiss(query_id)
                return
            message = callback.get("message") or {}
            here = str((message.get("chat") or {}).get("id") or "") or None
            what, value = chosen
            await self._dismiss(query_id)
            if what == "cancel":
                await self._close_card(message, here)
                return
            if what == "label":
                await self._label_task(value, here or "", message.get("message_thread_id"))
                return
            if what == "run":
                await self._run_command(value, here or "", message.get("message_thread_id"))
                return
            if what in ("inspect", "check"):
                # `check` is what a card said until 2026-09-23; its buttons still
                # work. Detached, as the command is: an inspection is a model turn
                # over a whole report, and this loop answers everybody else's.
                self._detach(
                    self._run_inspection(value, here or "", message.get("message_thread_id")),
                    "/inspect",
                )
                return
            if what == "result":
                await self._send_result(
                    value, f"tg:{user_id}", here or "", message.get("message_thread_id")
                )
                return
            if what in ("flow", "flowat"):
                # `flowat` is a step pressed: the workflow, then its place.
                name, _, index = value.rpartition(">") if what == "flowat" else (value, "", "")
                self._detach(
                    self._run_workflow(
                        name,
                        here or "",
                        message.get("message_thread_id"),
                        f"tg:{user_id}",
                        start=int(index) if what == "flowat" and index.isdigit() else None,
                    ),
                    "/workflow",
                )
                return
            if what == "pick":
                # Detached: it writes to the task's tracker, then runs whatever
                # needed the label — a handoff, a command, a workflow's step.
                self._detach(
                    self._label_picked(
                        value, here or "", message.get("message_thread_id"), f"tg:{user_id}"
                    ),
                    "/pick",
                )
                return
            if what in ("flowgo", "flowstop", "flowon", "flowpick"):
                self._detach(
                    self._resume_workflow(
                        what.removeprefix("flow"), here or "", message.get("message_thread_id")
                    ),
                    "/workflow",
                )
                return
            if what in ("handoff", "handto"):
                name, _, label = value.partition(">")
                self._detach(
                    self._run_handoff(
                        name,
                        here or "",
                        message.get("message_thread_id"),
                        f"tg:{user_id}",
                        to=label or None,
                    ),
                    "/handoff",
                )
                return
            if what == "open":
                await self._open_application(value, here or "", message.get("message_thread_id"))
                return
            if what == "fwd":
                await self._forward_last(
                    value, f"tg:{user_id}", here or "", message.get("message_thread_id")
                )
                return
            if what == "to":
                carried = ((message.get("reply_to_message") or {}).get("text") or "").strip()
                # The anchor may be the command itself, in which case the text
                # is everything after `/to`.
                if carried.startswith("/to"):
                    carried = carried.partition(" ")[2].strip()
                if _is_our_own_prompt(carried):
                    # Halyard's own words are not somebody's message.
                    #
                    # The seat menu is posted as a reply, and the client is free
                    # to anchor it to whatever is nearby — which on a second
                    # attempt was the "Send what to nav?" left over from the
                    # first. That question was then carried as the sentence to
                    # hand over, so Halyard asked an agent its own question and
                    # the agent, reasonably, said it did not understand.
                    carried = ""
                if not carried:
                    # No message to carry — the seat was picked from a bare
                    # `/to`. Ask for the text, naming the seat in the question
                    # so the answer arrives knowing where it goes.
                    await self._ask_for_text(
                        value, here or "", message.get("message_thread_id"), user_id
                    )
                    return
                # Said here as well as on the typed path. This one had no line
                # at all, so a message handed over by a button went wherever it
                # went without a trace, and working out where took a screenshot.
                logger.info("Handing to seat %s from tg:%s: %r", value, user_id, carried[:60])
                await self._forward_to_seat(
                    f"{value} {carried}",
                    f"tg:{user_id}",
                    here or "",
                    message.get("message_thread_id"),
                )
                return
            await self._choose(what, value, here, message.get("message_thread_id"))
            return

        asked = cards.parse_question_data(callback.get("data") or "")
        if asked is not None:
            await self._answer_question(asked, user_id, query_id)
            return

        proposed = commit_card.parse_callback_data(callback.get("data") or "")
        if proposed is not None:
            # Detached like the command that made the card: `Commit & push`
            # reaches a network, and a poller waiting on that answers nobody.
            self._detach(
                self._decide_commit(proposed, user_id, query_id, callback), "commit button"
            )
            return

        parsed = cards.parse_callback_data(callback.get("data") or "")

        if parsed is None:
            await self._dismiss(query_id)
            return

        handle, nonce, action = parsed
        entry = self._open.get(handle)
        request_id = entry[0].request_id if entry else None

        if user_id not in self._authorized:
            # Recorded, then ignored. No message back that would confirm the
            # request exists, or that this bot has anything to do with it.
            await self._record(
                unauthorized_callback(
                    actor=f"tg:{user_id}", request_id=request_id, channel="telegram"
                )
            )
            logger.warning("Ignoring callback from unauthorized Telegram user %s", user_id)
            await self._dismiss(query_id)
            return

        if entry is None:
            await self._dismiss(query_id, "That request is no longer open.")
            return

        request, message_id, chat_id, thread_id = entry

        if action == cards.SHOW_FULL:
            # To the chat the card is in. `chat_id` came off the pending entry,
            # which is where the question was asked and where somebody is
            # looking right now.
            await self._put_long_content(chat_id, thread_id, request.command_full, "Full command")
            await self._dismiss(query_id)
            return

        decision = Decision.ALLOW if action == cards.ALLOW else Decision.DENY
        actor = f"tg:{user_id}"

        try:
            await self._store.resolve(
                request.request_id, nonce=nonce, decision=decision, decided_by=actor
            )
        except AlreadyResolvedError:
            await self._record(replayed_callback(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id, "Already decided.")
            return
        except InvalidNonceError:
            await self._record(invalid_nonce(actor=actor, request_id=request.request_id))
            logger.warning("Callback for %s carried a bad nonce", request.request_id)
            await self._dismiss(query_id)
            return
        except ApprovalExpiredError:
            await self._settle_card(request, message_id, chat_id, "deny", None)
            await self._dismiss(query_id, "Too late — that expired and was denied.")
            return
        except UnknownApprovalError:
            # The store has evicted it, so nothing here can be resolved again.
            self._open.pop(handle, None)
            await self._dismiss(query_id, "That request is no longer open.")
            return

        if action == cards.STOP:
            # Refused like any denial, and then the inspection that asked is ended:
            # Deny alone leaves it free to try the next thing, which is exactly
            # what somebody pressing Stop wants to stop.
            await self._settle_card(request, message_id, chat_id, "stop", actor)
            await self._stop_inspection(request.session_id, actor)
            await self._dismiss(query_id, "Stopped.")
            return
        await self._settle_card(request, message_id, chat_id, decision.value, actor)
        await self._dismiss(query_id, "Allowed." if decision is Decision.ALLOW else "Denied.")

    # --- helpers ------------------------------------------------------------

    def _backoff(self, failures: int) -> float:
        """Wait longer as failures pile up, but never longer than the cap."""
        return min(self._poll_retry_seconds * (2 ** (failures - 1)), POLL_RETRY_MAX_SECONDS)

    async def _settle_card(
        self,
        request: ApprovalRequest,
        message_id: int,
        chat_id: str,
        decision: str,
        by: str | None,
    ) -> None:
        """Rewrite the card to show the outcome and drop the buttons."""
        running = self._inspecting.get(request.session_id)
        try:
            await self._api.edit_message_text(
                chat_id,
                message_id,
                cards.render_resolved(
                    request, decision=decision, by=by, inspection=running.name if running else None
                ),
                reply_markup=None,
            )
        except Exception:
            # Cosmetic. The decision is already recorded and the nonce is spent,
            # so a stale-looking card is untidy rather than dangerous.
            logger.warning("Could not update the card for %s", request.request_id, exc_info=True)

    async def _close_card(self, message: dict, chat_id: str | None) -> None:
        """Take the buttons off a choice card, and say it was cancelled.

        The text stays, so the chat still shows what was offered; the buttons
        go, so nothing on it can be pressed by mistake afterwards.
        """
        message_id = message.get("message_id")
        if not chat_id or not message_id:
            return
        shown = html.escape((message.get("text") or "").strip())
        try:
            await self._api.edit_message_text(
                chat_id, message_id, f"{shown}\n\n✖️ Cancelled" if shown else "✖️ Cancelled"
            )
        except Exception:
            logger.debug("Could not close a choice card", exc_info=True)

    async def _dismiss(self, query_id: str, text: str | None = None) -> None:
        if not query_id:
            return
        try:
            await self._api.answer_callback_query(query_id, text=text)
        except Exception:
            logger.debug("Could not answer callback query %s", query_id, exc_info=True)

    async def _record(self, record) -> None:
        try:
            await self._audit.record(record)
        except Exception:
            logger.exception("Could not record %s", record.action.value)

    async def _answer_question(
        self, asked: tuple[str, str, int], user_id: str, query_id: str
    ) -> None:
        """Resolve a question from a tapped option."""
        handle, nonce, index = asked
        entry = self._open_questions.get(handle)
        request_id = entry[0].request_id if entry else None

        if user_id not in self._authorized:
            await self._record(
                unauthorized_callback(
                    actor=f"tg:{user_id}", request_id=request_id, channel="telegram"
                )
            )
            await self._dismiss(query_id)
            return

        if entry is None:
            await self._dismiss(query_id, "That question is no longer open.")
            return

        request, message_id, chat_id = entry
        if not 0 <= index < len(request.options):
            # A button that names an option the request does not have. Nothing
            # to answer with, and guessing would be the wrong kind of helpful.
            await self._dismiss(query_id)
            return

        answer = request.options[index].label
        await self._resolve_question(
            request, message_id, chat_id, nonce, answer, f"tg:{user_id}", handle, query_id
        )

    async def _answer_open_question_with_text(self, here: str, text: str, user_id: str) -> bool:
        """Answer the question open in this chat with a typed reply.

        The "Other" path the tool always offers, and the reason a question card
        says *reply with your own words*. The session is blocked on the question,
        so a message typed into this chat is the answer to it rather than a new
        turn — which could not be delivered anyway while the turn is paused.

        Returns whether a question was answered, so the caller knows not to treat
        the message as ordinary chat.
        """
        entry = self._oldest_open_question_in(here)
        if entry is None:
            return False
        handle, (request, message_id, chat_id) = entry
        # The request's own nonce: authorisation here is "an allowed user, and a
        # question open in the chat it was asked in", which the caller already
        # checked. The nonce guards a public button against replay; a typed
        # answer is not that.
        await self._resolve_question(
            request, message_id, chat_id, request.nonce, text, f"tg:{user_id}", handle, None
        )
        return True

    async def _resolve_question(
        self,
        request: QuestionRequest,
        message_id: int,
        chat_id: str,
        nonce: str,
        answer: str,
        actor: str,
        handle: str,
        query_id: str | None,
    ) -> None:
        """Record an answer and settle the card, whichever way it arrived."""
        if self._question_store is None:
            return
        try:
            await self._question_store.answer(
                request.request_id, nonce=nonce, answer=answer, decided_by=actor
            )
        except AlreadyAnsweredError:
            await self._record(replayed_callback(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id, "Already answered.")
            return
        except QuestionInvalidNonceError:
            await self._record(invalid_nonce(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id)
            return
        except QuestionExpiredError:
            await self._settle_question_card(request, message_id, chat_id, None, None)
            self._open_questions.pop(handle, None)
            await self._dismiss(query_id, "Too late — that went back to the terminal.")
            return
        except UnknownQuestionError:
            self._open_questions.pop(handle, None)
            await self._dismiss(query_id, "That question is no longer open.")
            return

        self._open_questions.pop(handle, None)
        await self._settle_question_card(request, message_id, chat_id, answer, actor)
        await self._dismiss(query_id, "Answered.")

    async def _settle_question_card(
        self,
        request: QuestionRequest,
        message_id: int,
        chat_id: str,
        answer: str | None,
        by: str | None,
    ) -> None:
        try:
            await self._api.edit_message_text(
                chat_id,
                message_id,
                cards.render_question_resolved(request, answer=answer, by=by),
                reply_markup=None,
            )
        except Exception:
            logger.warning(
                "Could not update the question card for %s", request.request_id, exc_info=True
            )

    def _oldest_open_question_in(
        self, chat_id: str
    ) -> tuple[str, tuple[QuestionRequest, int, str]] | None:
        """The longest-waiting question routed to this chat, if any."""
        self._forget_expired_questions()
        candidates = [
            (handle, entry) for handle, entry in self._open_questions.items() if entry[2] == chat_id
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1][0].created_at)

    def _forget_expired_questions(self) -> None:
        now = self._clock()
        for handle in [h for h, (r, _, _) in self._open_questions.items() if now >= r.expires_at]:
            del self._open_questions[handle]

    def _forget_expired(self) -> None:
        """Drop cards that can no longer be acted on.

        Decided requests are deliberately *not* dropped here. Forgetting one the
        moment it is answered would make a second press look like a press on
        something unknown, and it would go unrecorded — but a button being
        pressed twice is exactly the kind of thing an audit log exists for. They
        are held until their deadline passes, by which point the store refuses
        them anyway.
        """
        now = self._clock()
        for handle in [h for h, (r, _, _, _) in self._open.items() if now >= r.expires_at]:
            del self._open[handle]
