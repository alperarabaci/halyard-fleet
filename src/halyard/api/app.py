"""The FastAPI application.

Thin on purpose. The endpoint parses a request, hands it to `ApprovalService`,
and turns the answer into JSON. Every decision about what an answer should be
lives in core, where it can be tested without a web server in the way.

One rule shapes this module: **a hook bridge must always receive a decision it
can act on.** Claude Code runs the command when a hook fails to answer cleanly,
so an unhandled exception here would eventually become an approval. The service
does not raise; the middleware below catches whatever still could.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from halyard import __version__
from halyard.agents import registry as runtimes
from halyard.channels.stub import StubChannel
from halyard.channels.telegram import TelegramApi, TelegramChannel
from halyard.config import ChannelKind, Settings
from halyard.core import compaction as after_compaction
from halyard.core import credentials
from halyard.core import tools as configured_tools
from halyard.core import writes as configured_writes
from halyard.core.approvals import ApprovalStore, Decision
from halyard.core.audit import (
    AuditAction,
    AuditLog,
    AuditRecord,
    JsonlAuditSink,
    SqliteAuditSink,
)
from halyard.core.config_file import missing_files
from halyard.core.events import RiskLevel, Role
from halyard.core.gate import Gate
from halyard.core.policy import Policy
from halyard.core.questions import Choice, QuestionStore
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry
from halyard.core.seats import configured
from halyard.core.service import (
    ApprovalService,
    BridgeDecision,
    MessageRelay,
    QuestionService,
)
from halyard.core.transcripts import TranscriptWatcher

logger = logging.getLogger(__name__)


def configured_projects() -> dict:
    """Each configured project by name, or nothing if the file cannot be read.

    Read defensively for the same reason `prompts:` is: a mistake in the part of
    the configuration a person edits often must not take the gate down with it.

    Projects without a `path:` are dropped — a project can be named and given
    seats before anybody decides where its code lives, and everything that uses
    this needs somewhere to look.
    """
    from halyard.core.config_file import projects as described

    try:
        return {p.name: p for p in described() if p.path}
    except Exception:
        logger.warning("Could not read `projects:`", exc_info=True)
        return {}


class ApprovalRequestBody(BaseModel):
    """What the hook bridge posts.

    Deliberately close to a raw hook payload: the bridge's job is to be too
    simple to get anything wrong, so translation happens here rather than there.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    tool: str
    command: str
    agent_id: str = runtimes.DEFAULT
    tool_use_id: str | None = None
    cwd: str | None = None
    #: The session's project root, so a card can name the codebase a command
    #: came from rather than whatever one name the control plane was configured
    #: with. See `project_name` in core.
    project_dir: str | None = None
    role: Role | None = None
    #: The session's name in the desktop app, where there is no shell to set
    #: HALYARD_ROLE in. Matched against the configured seats.
    session_name: str | None = None
    reason: str | None = None
    #: What the agent says about its own call. Can raise the risk, never lower
    #: it — see `policy.py`.
    declared_risk: RiskLevel | None = None
    #: The destination of a file tool, matched against the `writes:` block to
    #: decide whether this one may go through without a card.
    file_path: str | None = None


class ApprovalResponse(BaseModel):
    """What the bridge turns into a hook decision.

    Three values, where an approval only ever has two. `defer` means no
    approval happened at all — the gate is paused, so Halyard steps aside and
    Claude Code decides on its own, exactly as if the hook were not installed.
    """

    decision: BridgeDecision
    reason: str
    request_id: str | None = None
    risk: RiskLevel | None = None


class QuestionOptionBody(BaseModel):
    """One option the agent offered, straight off the `AskUserQuestion` input."""

    model_config = ConfigDict(extra="ignore")

    label: str
    description: str | None = None


class QuestionRequestBody(BaseModel):
    """What the bridge posts when it sees an `AskUserQuestion`.

    A single question and its options — the MVP handles one at a time, and the
    bridge sends the rest back to the terminal rather than folding them together.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    question: str
    options: list[QuestionOptionBody]
    header: str | None = None
    agent_id: str = runtimes.DEFAULT
    tool_use_id: str | None = None
    cwd: str | None = None
    project_dir: str | None = None
    role: Role | None = None
    session_name: str | None = None


class QuestionResponse(BaseModel):
    """What the bridge fills into `updatedInput.answers`, or nothing.

    `answer` is null when nobody chose in time, the gate is paused, or delivery
    failed. The bridge reads that as "say nothing", and the terminal picker —
    which never went away — takes the choice. The opposite of the approval
    response, whose silence would be dangerous; here silence is the safe fallback.
    """

    answer: str | None = None


class MessageBody(BaseModel):
    """What the Stop-hook relay posts: whatever the agent just said."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    text: str
    agent_id: str = runtimes.DEFAULT
    cwd: str | None = None
    project_dir: str | None = None
    role: Role | None = None
    session_name: str | None = None


class CompactionBody(BaseModel):
    """What the `SessionStart` bridge asks after a compaction."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    agent_id: str = runtimes.DEFAULT
    session_name: str | None = None
    #: `before` starts the record while the summary is being made; `after`
    #: collects it. Anything else is treated as `after`, which only ever reads.
    when: str = "after"


class CompactionResponse(BaseModel):
    """The orientation to fold back into the session, or nothing.

    Empty is the ordinary answer: a seat with no `after_compaction:` file, or a
    session that resolves to no seat at all. The bridge prints nothing then, and
    the session carries on exactly as the compaction left it.
    """

    context: str | None = None


class MessageResponse(BaseModel):
    #: Whether the channel accepted it. The relay does not act on this — it is
    #: here so a failure is visible to anything that does look.
    delivered: bool


class InjectBody(BaseModel):
    """What the `PreInvocation` hook asks: is anything owed to this session?"""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    #: Whichever runtime asked. Only one queues anything, and the bridge always
    #: says which it is — a name written here would be this module knowing about
    #: a runtime, which is what `RuntimeSpec` exists to prevent.
    agent_id: str = runtimes.DEFAULT


class InjectResponse(BaseModel):
    """Antigravity's own shape, passed through by the bridge untouched.

    `injectSteps` with `{"userMessage": ...}` is the only way a message enters
    a conversation as a turn the person typed. `agentapi send-message` can file
    a `SYSTEM_MESSAGE` and nothing else.
    """

    injectSteps: list[dict] = Field(default_factory=list)  # noqa: N815 — their spelling


class HealthResponse(BaseModel):
    status: str = "ok"
    channel: str
    project: str
    open_approvals: int
    #: False when this control plane cannot send messages into a session —
    #: it has no claude CLI, which is what a container looks like.
    can_send_messages: bool = False
    #: Which runtime each seat is, so a Codex seat is visible from outside.
    seats: dict[str, str] = Field(default_factory=dict)
    #: True while approvals are not being relayed — Halyard has stepped aside
    #: and Claude Code is deciding on its own. Visible from outside for the same
    #: reason as the field below.
    paused: bool = False
    #: True when the configured channel answers by itself. Surfaced so it is
    #: possible to notice from outside that nobody is actually being asked.
    decides_without_a_human: bool = Field(default=False)


def _build_channel(
    settings: Settings,
    store: ApprovalStore,
    audit: AuditLog,
    gate: Gate,
    registry: SessionRegistry,
    runner,
    runners: dict | None = None,
    seats: list | None = None,
    question_store: QuestionStore | None = None,
):
    if settings.channel is ChannelKind.STUB_ALLOW:
        return StubChannel(store, Decision.ALLOW, question_store)
    if settings.channel is ChannelKind.STUB_DENY:
        return StubChannel(store, Decision.DENY, question_store)
    # Prompts are the one part of the configuration a person edits often, so a
    # mistake in them must not take the control plane down with it. Refusing to
    # start over the wording of a shortcut would lose the gate as well.
    from halyard.channels.telegram.adapter import COMMANDS
    from halyard.core import prompts as configured_prompts

    try:
        prompts = configured_prompts.load(reserved=[name for name, _ in COMMANDS])
    except ValueError as error:
        logger.warning("Ignoring the `prompts:` block: %s", error)
        prompts = dict(configured_prompts.DEFAULTS)

    # `Settings` has already refused to start if any of these are missing.
    return TelegramChannel(
        api=TelegramApi(settings.telegram_bot_token or ""),
        store=store,
        question_store=question_store,
        audit=audit,
        chat_id=settings.telegram_chat_id or "",
        authorized_user_ids=settings.telegram_authorized_user_ids,
        gate=gate,
        project=settings.project_name,
        navigator_chat_id=settings.telegram_navigator_chat_id,
        driver_chat_id=settings.telegram_driver_chat_id,
        registry=registry,
        runner=runner,
        runners=runners,
        seats=seats,
        prompts=prompts,
        # Where each project's code is, for `/commit`. Read here rather than in
        # the channel so a malformed `projects:` block cannot take the gate
        # down — the same reason `prompts:` is loaded defensively above.
        repositories=configured_projects(),
        forge_token=settings.forge_token,
        session_names={
            role: name
            for name, role in (
                (settings.navigator_session, Role.NAVIGATOR),
                (settings.driver_session, Role.DRIVER),
            )
            if name
        },
    )


def create_app(settings: Settings, *, channel=None) -> FastAPI:
    """Assemble the control plane.

    `channel` is injectable so tests can supply a double without going near the
    environment.
    """
    store = ApprovalStore(ttl=timedelta(seconds=settings.approval_timeout_seconds))
    # A question waits as long as an approval does, and against the same clock:
    # the store answers before the bridge's HTTP call gives up, which is before
    # the hook times out. Past it the question is unanswered and the terminal
    # picker takes over.
    question_store = QuestionStore(ttl=timedelta(seconds=settings.approval_timeout_seconds))
    audit = AuditLog([JsonlAuditSink(settings.audit_log), SqliteAuditSink(settings.db_path)])
    registry = SessionRegistry()
    gate = Gate()
    # Names are matched case-insensitively, so they are folded once here
    # rather than on every request.
    seats = {
        name.strip().casefold(): role
        for name, role in (
            (settings.navigator_session, Role.NAVIGATOR),
            (settings.driver_session, Role.DRIVER),
        )
        if name
    }
    # One runner per runtime, built once and shared by whichever seats use it.
    # A seat is a name plus the thing that knows what the name means: the same
    # `alpha-engine-driver` is a Claude Code session or a Codex thread
    # depending on HALYARD_DRIVER_RUNTIME, and the two keep their sessions in
    # entirely different places.
    by_runtime = {name: spec.runner(settings) for name, spec in runtimes.discover().items()}
    configured_seats = configured()
    # What `/health` and anything else with one question in mind should ask.
    runner = by_runtime[runtimes.DEFAULT]
    resolved_channel = (
        channel
        if channel is not None
        else _build_channel(
            settings,
            store,
            audit,
            gate,
            registry,
            runner,
            by_runtime,
            configured_seats,
            question_store,
        )
    )
    relay = MessageRelay(
        redactor=Redactor(),
        registry=registry,
        audit=audit,
        channel=resolved_channel,
        project=settings.project_name,
        gate=gate,
        seats=seats,
    )
    # Paths a write may reach without anybody being asked. Empty unless the
    # configuration says otherwise, and a block that will not parse is refused
    # loudly rather than read as "nothing" — a grant somebody believes they have
    # written and that is silently absent is the worse of the two failures.
    try:
        allowed_writes = configured_writes.load()
    except ValueError as error:
        logger.error("Ignoring the `writes:` block, so every write will ask: %s", error)
        allowed_writes = ()
    if allowed_writes:
        logger.info(
            "Writes to %s go through without asking", ", ".join(repr(p) for p in allowed_writes)
        )

    try:
        allowed_tools = configured_tools.load()
    except ValueError as error:
        logger.error("Ignoring the `tools:` block, so every one of them will ask: %s", error)
        allowed_tools = ()
    if allowed_tools:
        logger.info("Tools %s run without asking", ", ".join(repr(p) for p in allowed_tools))

    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=audit,
        registry=registry,
        channel=resolved_channel,
        project=settings.project_name,
        gate=gate,
        seats=seats,
        allowed_writes=allowed_writes,
        allowed_tools=allowed_tools,
        refuse_agent_commits=settings.refuse_agent_commits,
    )
    questions = QuestionService(
        store=question_store,
        audit=audit,
        registry=registry,
        channel=resolved_channel,
        project=settings.project_name,
        gate=gate,
        seats=seats,
    )
    # Notices a turn that died where no hook fires — an API error mid-turn does
    # not fire `Stop`, so the reply relay never runs. Entirely off the approval
    # path: fed session paths as a side effect of requests it does not touch.
    watcher = TranscriptWatcher(channel=resolved_channel, gate=gate)
    # Writes the record a compaction is about to make unrecoverable, in a turn
    # of its own so the session it is about is never resumed or forked.
    known_projects = configured_projects()
    # Prompt files live in the codebase they describe, so they are looked for
    # there rather than beside Halyard. See `compaction.in_project`.
    project_paths = {name: found.path for name, found in known_projects.items() if found.path}

    # Said at startup, because nothing said it before: a file named in the
    # configuration and not on disk produced a warning at the moment it was
    # needed, in a log nobody was reading, and a compaction that carried
    # nothing. One machine ran that way for weeks.
    for line in missing_files(list(known_projects.values())):
        logger.warning("%s", line)

    recorder = after_compaction.Recorder(
        seats=configured_seats,
        projects=project_paths,
        runners=by_runtime,
        model=settings.compaction_model,
        limit=settings.compaction_record_limit,
        channel=resolved_channel,
        gate=gate,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await audit.open()
        await resolved_channel.start()

        # How old the control plane's own credential is getting. Nothing
        # reports when a token expires — `claude auth status` answers with
        # eight fields and not one of them is a date — so this is an estimate
        # from when Halyard first saw it, and it says so in those words.
        #
        # Sent to the navigator rather than only logged: the failure it exists
        # to prevent happens while nobody is at the machine, and a warning in a
        # log is a warning for afterwards.
        aged = credentials.remember(
            settings.claude_oauth_token, settings.db_path.parent / "credential-seen.json"
        )
        if aged and aged.worth_saying(_now()):
            logger.warning("%s", aged.wording(_now()))
            with contextlib.suppress(Exception):
                await resolved_channel.send_message("halyard", aged.wording(_now()), Role.NAVIGATOR)
        # The watcher's own loop, alongside the channel's. A best-effort courier,
        # so a failure to start it is logged and shrugged off rather than kept
        # from serving.
        watch_task = asyncio.create_task(watcher.run(), name="transcript-watch")
        await audit.record(
            AuditRecord(
                action=AuditAction.CONTROL_PLANE_STARTED,
                recorded_at=_now(),
                actor="system",
                project=settings.project_name,
                detail={"channel": resolved_channel.name, "bind": settings.bind},
            )
        )
        try:
            yield
        finally:
            watch_task.cancel()
            # Order matters. Deny everything still open before the audit log
            # closes, so the denials are recorded — and so no bridge is left
            # waiting out its own timeout, past which the hook fails open.
            await store.shutdown()
            # Leave open questions unanswered so any bridge blocked on one is
            # told to fall back to the terminal rather than waiting out its
            # timeout. The fail-open twin of the denial above.
            await question_store.shutdown()
            try:
                await audit.record(
                    AuditRecord(
                        action=AuditAction.CONTROL_PLANE_STOPPED,
                        recorded_at=_now(),
                        actor="system",
                        project=settings.project_name,
                    )
                )
            finally:
                await resolved_channel.stop()
                await audit.close()

    app = FastAPI(
        title="Halyard Fleet",
        description="A control plane for orchestrating coding agents remotely.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.audit = audit
    app.state.registry = registry
    app.state.channel = resolved_channel
    app.state.service = service
    app.state.questions = questions
    app.state.relay = relay
    app.state.watcher = watcher
    app.state.gate = gate
    app.state.runner = runner
    # Every runtime, by name. `runner` above is the Claude Code one, kept for
    # callers with a single question in mind; anything that has to reach a
    # particular runtime — `/v1/inject` does — needs the whole set.
    app.state.runners = by_runtime

    @app.middleware("http")
    async def deny_on_unhandled_error(request: Request, call_next) -> Response:
        """Turn any escaped exception on the approval path into a denial.

        Middleware rather than `@app.exception_handler(Exception)`. That
        decorator hands the exception to Starlette's `ServerErrorMiddleware`,
        which sends the response and then **re-raises** so the server can log
        it. The bridge does still receive the body, but a fail-closed guarantee
        that depends on "the response was already written before the traceback
        propagated" is too subtle to rest a security property on. Catching here
        ends the exception instead of stepping around it.

        Scoped to the approval path. A `/health` request that blows up should
        say so with a 500 rather than answer with a decision about nothing.
        """
        try:
            return await call_next(request)
        except Exception:
            logger.exception("Unhandled error on %s", request.url.path)
            if not request.url.path.startswith("/v1/approvals"):
                return JSONResponse(status_code=500, content={"detail": "internal error"})
            return JSONResponse(
                status_code=200,
                content=ApprovalResponse(
                    decision=BridgeDecision.DENY,
                    reason=(
                        "Denied: the Halyard control plane hit an internal error and failed "
                        "closed. Nothing was approved."
                    ),
                ).model_dump(mode="json"),
            )

    @app.post("/v1/approvals", response_model=ApprovalResponse)
    async def request_approval(body: ApprovalRequestBody) -> ApprovalResponse:
        """Block until the request is decided, then answer.

        Held open for as long as the approval deadline allows. The bridge's own
        HTTP timeout sits above that, and the hook timeout above both.
        """
        # A side effect that touches nothing here decides: the watcher learns
        # where this active session's transcript is. `note` never raises.
        watcher.note(
            session_id=body.session_id,
            agent_id=body.agent_id,
            role=body.role,
            session_name=body.session_name,
        )
        outcome = await service.request(
            session_id=body.session_id,
            agent_id=body.agent_id,
            tool=body.tool,
            command=body.command,
            tool_use_id=body.tool_use_id,
            cwd=body.cwd,
            project_dir=body.project_dir,
            role=body.role,
            session_name=body.session_name,
            reason=body.reason,
            declared_risk=body.declared_risk,
            file_path=body.file_path,
        )
        return ApprovalResponse(
            decision=outcome.decision,
            reason=outcome.reason,
            request_id=outcome.request_id,
            risk=outcome.risk,
        )

    @app.post("/v1/questions", response_model=QuestionResponse)
    async def ask_question(body: QuestionRequestBody) -> QuestionResponse:
        """Block until a person chooses, then answer — or fall back to the desk.

        Held open for the same deadline as an approval, and above it sits the
        bridge's HTTP timeout. Unlike `/v1/approvals`, an empty answer is safe:
        it sends the choice back to the terminal picker rather than deciding it.
        """
        outcome = await questions.ask(
            session_id=body.session_id,
            agent_id=body.agent_id,
            question=body.question,
            options=[Choice(label=o.label, description=o.description) for o in body.options],
            header=body.header,
            tool_use_id=body.tool_use_id,
            cwd=body.cwd,
            project_dir=body.project_dir,
            role=body.role,
            session_name=body.session_name,
        )
        return QuestionResponse(answer=outcome.answer)

    @app.post("/v1/compaction", response_model=CompactionResponse)
    async def compaction_context(body: CompactionBody) -> CompactionResponse:
        """What to tell a session that has just been compacted.

        Answers immediately and never blocks: the session is waiting on this
        before it does anything. Nothing here decides anything, so every
        uncertainty answers with nothing rather than guessing.
        """
        if body.when == "before":
            # Held open while the record is written. The compaction waits for
            # it, which is the trade taken deliberately: a summary that starts a
            # minute later beats a record that arrives after the context it was
            # about is gone. Bounded in `Recorder`, and every failure there ends
            # in the compaction simply going ahead.
            await recorder.write(
                session_id=body.session_id,
                agent_id=body.agent_id,
                session_name=body.session_name,
            )
            return CompactionResponse()

        # What the seat says to read, and what this session knew before the
        # summary took it. Either may be missing; both missing means the bridge
        # prints nothing and the session carries on as compaction left it.
        standing = after_compaction.for_seat(
            configured_seats,
            agent_id=body.agent_id,
            session_name=body.session_name,
            session_id=body.session_id,
            projects=project_paths,
        )
        record = await recorder.take(
            body.session_id, agent_id=body.agent_id, session_name=body.session_name
        )
        parts = [
            part
            for part in (
                f"Record of this session before it was compacted:\n\n{record}" if record else None,
                standing,
            )
            if part
        ]
        return CompactionResponse(context="\n\n---\n\n".join(parts) or None)

    @app.post("/v1/messages", response_model=MessageResponse)
    async def relay_message(body: MessageBody) -> MessageResponse:
        """Push an agent's reply out to the channel.

        Answers immediately and never blocks — the agent's turn is waiting on
        this call, and a chat message is not worth stalling a session for. The
        opposite of `/v1/approvals`, which holds the caller until a human
        decides.
        """
        watcher.note(
            session_id=body.session_id,
            agent_id=body.agent_id,
            role=body.role,
            session_name=body.session_name,
        )
        delivered = await relay.relay(
            session_id=body.session_id,
            agent_id=body.agent_id,
            text=body.text,
            cwd=body.cwd,
            project_dir=body.project_dir,
            role=body.role,
            session_name=body.session_name,
        )
        return MessageResponse(delivered=delivered)

    @app.post("/v1/inject", response_model=InjectResponse)
    async def inject(body: InjectBody) -> InjectResponse:
        """Hand over whatever a session is owed, once.

        Answers immediately. A model call is blocked behind this, and every
        invocation in every Antigravity session pays the round trip — so it
        does no work beyond emptying a list.

        Only a runtime that has somewhere to put messages is asked. The other
        two deliver a real user turn directly and never queue anything, so a
        request naming one of them is answered with nothing rather than with
        an error: a hook that gets an error is a hook that logs a failure on
        every turn for a runtime that was never involved.
        """
        runner = by_runtime.get(body.agent_id)
        waiting = runner.take_pending(body.session_id) if hasattr(runner, "take_pending") else []
        return InjectResponse(injectSteps=[{"userMessage": text} for text in waiting])

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            channel=resolved_channel.name,
            project=settings.project_name,
            open_approvals=len(await store.list_open()),
            paused=gate.paused,
            can_send_messages=runner.available,
            seats={seat.label: seat.runtime for seat in configured_seats},
            decides_without_a_human=settings.channel.decides_without_a_human,
        )

    return app


def _now() -> datetime:
    return datetime.now(UTC)
