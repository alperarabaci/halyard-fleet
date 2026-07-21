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

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from halyard.channels.stub import StubChannel
from halyard.channels.telegram import TelegramApi, TelegramChannel
from halyard.config import ChannelKind, Settings
from halyard.core.approvals import ApprovalStore, Decision
from halyard.core.audit import (
    AuditAction,
    AuditLog,
    AuditRecord,
    JsonlAuditSink,
    SqliteAuditSink,
)
from halyard.core.events import RiskLevel, Role
from halyard.core.gate import Gate
from halyard.core.policy import Policy
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry
from halyard.core.service import ApprovalService, BridgeDecision, MessageRelay

logger = logging.getLogger(__name__)


class ApprovalRequestBody(BaseModel):
    """What the hook bridge posts.

    Deliberately close to a raw hook payload: the bridge's job is to be too
    simple to get anything wrong, so translation happens here rather than there.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    tool: str
    command: str
    agent_id: str = "claude-code"
    tool_use_id: str | None = None
    cwd: str | None = None
    #: The session's project root, so a card can name the codebase a command
    #: came from rather than whatever one name the control plane was configured
    #: with. See `project_name` in core.
    project_dir: str | None = None
    role: Role | None = None
    reason: str | None = None
    #: What the agent says about its own call. Can raise the risk, never lower
    #: it — see `policy.py`.
    declared_risk: RiskLevel | None = None


class ApprovalResponse(BaseModel):
    """What the bridge turns into a hook decision.

    Three values, where an approval only ever has two. `defer` means no
    approval happened at all — the gate is paused and Claude Code should ask in
    the terminal instead.
    """

    decision: BridgeDecision
    reason: str
    request_id: str | None = None
    risk: RiskLevel | None = None


class MessageBody(BaseModel):
    """What the Stop-hook relay posts: whatever the agent just said."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    text: str
    agent_id: str = "claude-code"
    cwd: str | None = None
    project_dir: str | None = None
    role: Role | None = None


class MessageResponse(BaseModel):
    #: Whether the channel accepted it. The relay does not act on this — it is
    #: here so a failure is visible to anything that does look.
    delivered: bool


class HealthResponse(BaseModel):
    status: str = "ok"
    channel: str
    project: str
    open_approvals: int
    #: True while approvals are not being relayed — the terminal is asking
    #: instead. Visible from outside for the same reason as the field below.
    paused: bool = False
    #: True when the configured channel answers by itself. Surfaced so it is
    #: possible to notice from outside that nobody is actually being asked.
    decides_without_a_human: bool = Field(default=False)


def _build_channel(settings: Settings, store: ApprovalStore, audit: AuditLog, gate: Gate):
    if settings.channel is ChannelKind.STUB_ALLOW:
        return StubChannel(store, Decision.ALLOW)
    if settings.channel is ChannelKind.STUB_DENY:
        return StubChannel(store, Decision.DENY)
    # `Settings` has already refused to start if any of these are missing.
    return TelegramChannel(
        api=TelegramApi(settings.telegram_bot_token or ""),
        store=store,
        audit=audit,
        chat_id=settings.telegram_chat_id or "",
        authorized_user_ids=settings.telegram_authorized_user_ids,
        gate=gate,
        project=settings.project_name,
    )


def create_app(settings: Settings, *, channel=None) -> FastAPI:
    """Assemble the control plane.

    `channel` is injectable so tests can supply a double without going near the
    environment.
    """
    store = ApprovalStore(ttl=timedelta(seconds=settings.approval_timeout_seconds))
    audit = AuditLog([JsonlAuditSink(settings.audit_log), SqliteAuditSink(settings.db_path)])
    registry = SessionRegistry()
    gate = Gate()
    resolved_channel = (
        channel if channel is not None else _build_channel(settings, store, audit, gate)
    )
    relay = MessageRelay(
        redactor=Redactor(),
        registry=registry,
        audit=audit,
        channel=resolved_channel,
        project=settings.project_name,
    )
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=audit,
        registry=registry,
        channel=resolved_channel,
        project=settings.project_name,
        gate=gate,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await audit.open()
        await resolved_channel.start()
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
            # Order matters. Deny everything still open before the audit log
            # closes, so the denials are recorded — and so no bridge is left
            # waiting out its own timeout, past which the hook fails open.
            await store.shutdown()
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
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.audit = audit
    app.state.registry = registry
    app.state.channel = resolved_channel
    app.state.service = service
    app.state.relay = relay
    app.state.gate = gate

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
        outcome = await service.request(
            session_id=body.session_id,
            agent_id=body.agent_id,
            tool=body.tool,
            command=body.command,
            tool_use_id=body.tool_use_id,
            cwd=body.cwd,
            project_dir=body.project_dir,
            role=body.role,
            reason=body.reason,
            declared_risk=body.declared_risk,
        )
        return ApprovalResponse(
            decision=outcome.decision,
            reason=outcome.reason,
            request_id=outcome.request_id,
            risk=outcome.risk,
        )

    @app.post("/v1/messages", response_model=MessageResponse)
    async def relay_message(body: MessageBody) -> MessageResponse:
        """Push an agent's reply out to the channel.

        Answers immediately and never blocks — the agent's turn is waiting on
        this call, and a chat message is not worth stalling a session for. The
        opposite of `/v1/approvals`, which holds the caller until a human
        decides.
        """
        delivered = await relay.relay(
            session_id=body.session_id,
            agent_id=body.agent_id,
            text=body.text,
            cwd=body.cwd,
            project_dir=body.project_dir,
            role=body.role,
        )
        return MessageResponse(delivered=delivered)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            channel=resolved_channel.name,
            project=settings.project_name,
            open_approvals=len(await store.list_open()),
            paused=gate.paused,
            decides_without_a_human=settings.channel.decides_without_a_human,
        )

    return app


def _now() -> datetime:
    return datetime.now(UTC)
