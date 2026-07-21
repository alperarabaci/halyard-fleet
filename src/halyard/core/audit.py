"""The append-only record of every decision the control plane made.

Two sinks, on purpose. SQLite is what the control plane queries; a JSONL file is
what a human reads at three in the morning when they want to know what was
approved while they were asleep, without a database client and without trusting
the process that wrote it. Losing one should not cost you the other.

Append-only is enforced by the storage engine rather than by discipline: the
SQLite schema installs triggers that abort any UPDATE or DELETE against the
table. An audit log that the application *could* rewrite is a log that says only
what the last writer wanted it to say.

Everything handed to this module must already be redacted. `redaction.py` runs
at the edge, before core builds an `ApprovalRequest`, so by the time a record is
assembled here there is no unmasked secret left to leak. Nothing in this module
re-checks that, which is exactly why the edge has to be the one place it happens.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from halyard.core.approvals import ApprovalRequest, ApprovalResolution

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


class AuditAction(StrEnum):
    """Everything worth being able to reconstruct after the fact."""

    #: An agent asked for permission and a card went out.
    APPROVAL_REQUESTED = "approval.requested"
    #: A decision was reached, by a human or by the deadline.
    APPROVAL_RESOLVED = "approval.resolved"
    #: A callback arrived from someone not on the authorized list.
    UNAUTHORIZED_CALLBACK = "callback.unauthorized"
    #: A callback arrived with a nonce that did not match.
    INVALID_NONCE = "callback.invalid_nonce"
    #: A button was pressed a second time.
    REPLAYED_CALLBACK = "callback.replayed"
    #: The bridge could not complete a request and failed closed.
    BRIDGE_ERROR = "bridge.error"
    #: An agent's reply was relayed out to a channel. Metadata only — see
    #: `agent_message` for why the text itself is not kept here.
    AGENT_MESSAGE = "agent.message"
    CONTROL_PLANE_STARTED = "control_plane.started"
    CONTROL_PLANE_STOPPED = "control_plane.stopped"


class AuditRecord(BaseModel):
    """One thing that happened. Immutable, like the log it goes into."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AuditAction
    recorded_at: datetime
    #: Whoever caused this — a channel-scoped user id, `"system"`, `"bridge"`.
    actor: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    project: str | None = None
    #: Already-redacted context. Free-form so a new action does not require a
    #: schema migration, at the cost of being the caller's job to keep sensible.
    detail: dict[str, Any] = Field(default_factory=dict)
    record_id: str = Field(default_factory=lambda: f"aud_{uuid4().hex}")


# --- record factories -------------------------------------------------------
#
# Call sites build records through these rather than assembling them by hand, so
# the same event always lands in the log with the same shape. A query that has
# to guess which of three spellings a field took is a query nobody writes.


def approval_requested(request: ApprovalRequest, *, now: datetime | None = None) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.APPROVAL_REQUESTED,
        recorded_at=now or _default_clock(),
        actor=request.agent_id,
        request_id=request.request_id,
        session_id=request.session_id,
        agent_id=request.agent_id,
        project=request.project,
        detail={
            "tool": request.tool,
            "command": request.command_full,
            "risk": request.risk.value,
            "role": request.role.value if request.role else None,
            "reason": request.reason,
            "tool_use_id": request.tool_use_id,
            "expires_at": request.expires_at.isoformat(),
        },
    )


def approval_resolved(
    request: ApprovalRequest,
    resolution: ApprovalResolution,
    *,
    now: datetime | None = None,
) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.APPROVAL_RESOLVED,
        recorded_at=now or resolution.decided_at,
        actor=resolution.decided_by or "system",
        request_id=request.request_id,
        session_id=request.session_id,
        agent_id=request.agent_id,
        project=request.project,
        detail={
            "decision": resolution.decision.value,
            "reason": resolution.reason.value,
            "note": resolution.note,
            "tool": request.tool,
            # Repeated from the request record on purpose. A reader scanning
            # decisions should not have to join back to another line to find out
            # what was actually decided.
            "command": request.command_full,
            "risk": request.risk.value,
        },
    )


def unauthorized_callback(
    *,
    actor: str,
    request_id: str | None = None,
    channel: str | None = None,
    now: datetime | None = None,
) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.UNAUTHORIZED_CALLBACK,
        recorded_at=now or _default_clock(),
        actor=actor,
        request_id=request_id,
        detail={"channel": channel},
    )


def invalid_nonce(
    *, actor: str | None, request_id: str, now: datetime | None = None
) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.INVALID_NONCE,
        recorded_at=now or _default_clock(),
        actor=actor,
        request_id=request_id,
    )


def replayed_callback(
    *, actor: str | None, request_id: str, now: datetime | None = None
) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.REPLAYED_CALLBACK,
        recorded_at=now or _default_clock(),
        actor=actor,
        request_id=request_id,
    )


def agent_message(
    *,
    session_id: str,
    agent_id: str,
    project: str,
    length: int,
    redacted: bool,
    delivered: bool,
    now: datetime | None = None,
) -> AuditRecord:
    """Record that an agent said something and it went out to a channel.

    The text is deliberately not stored. This log is the permanent, append-only
    record of *decisions*, and an assistant's conversation is not one — copying
    it here would grow the permanent record without bound and fill it with
    content nobody ever reviewed. The chat is where the conversation lives; this
    line exists so that "was anything relayed at 03:14, and did it get through"
    has an answer.
    """
    return AuditRecord(
        action=AuditAction.AGENT_MESSAGE,
        recorded_at=now or _default_clock(),
        actor=agent_id,
        session_id=session_id,
        agent_id=agent_id,
        project=project,
        detail={"length": length, "redacted": redacted, "delivered": delivered},
    )


def bridge_error(
    *,
    message: str,
    session_id: str | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
) -> AuditRecord:
    return AuditRecord(
        action=AuditAction.BRIDGE_ERROR,
        recorded_at=now or _default_clock(),
        actor="bridge",
        session_id=session_id,
        request_id=request_id,
        detail={"message": message},
    )


# --- sinks ------------------------------------------------------------------


class AuditSink(Protocol):
    """Somewhere records go. Writes only; there is no update and no delete."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def write(self, record: AuditRecord) -> None: ...


class JsonlAuditSink:
    """Appends one JSON object per line to a file a human can read.

    The handle stays open and every record is flushed as it is written, so a
    process that dies still leaves behind everything it had already decided.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: Any = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode, always. Truncating an audit log on startup would make
        # "no record of it" the cheapest way to erase an inconvenient decision.
        self._handle = self._path.open("a", encoding="utf-8")

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    async def write(self, record: AuditRecord) -> None:
        if self._handle is None:
            raise RuntimeError("JsonlAuditSink.open() must be awaited before writing")
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        # Serialised so two concurrent writers cannot interleave halves of two
        # records into one unparseable line.
        async with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    async def read_all(self) -> list[AuditRecord]:
        """Read the file back. Used by tests and by anything verifying the log."""
        if not self._path.exists():
            return []
        return [
            AuditRecord.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


#: Types are deliberately plain so this schema survives the move to Postgres:
#: TEXT becomes TEXT, the JSON column becomes JSONB, and AUTOINCREMENT becomes
#: an identity column. AUTOINCREMENT rather than a bare rowid because it
#: guarantees ids are never reused, so a deleted row leaves a visible gap.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   TEXT NOT NULL UNIQUE,
    action      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor       TEXT,
    request_id  TEXT,
    session_id  TEXT,
    agent_id    TEXT,
    project     TEXT,
    detail      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_log_request_id_idx ON audit_log (request_id);
CREATE INDEX IF NOT EXISTS audit_log_recorded_at_idx ON audit_log (recorded_at);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""


class SqliteAuditSink:
    """The queryable copy, with append-only enforced by the database itself."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        # WAL so a reader tailing the log never blocks a decision being written.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def write(self, record: AuditRecord) -> None:
        if self._db is None:
            raise RuntimeError("SqliteAuditSink.open() must be awaited before writing")
        # Serialised through the same path the JSONL sink uses, so the two
        # copies can never disagree about how a value is spelled. Hand-rolling
        # the conversion here is how the file ends up saying "…18:00:00Z" while
        # the database says "…18:00:00+00:00" for the same instant.
        data = record.model_dump(mode="json")
        await self._db.execute(
            """
            INSERT INTO audit_log
                (record_id, action, recorded_at, actor,
                 request_id, session_id, agent_id, project, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["record_id"],
                data["action"],
                data["recorded_at"],
                data["actor"],
                data["request_id"],
                data["session_id"],
                data["agent_id"],
                data["project"],
                json.dumps(data["detail"], ensure_ascii=False),
            ),
        )
        await self._db.commit()

    async def read_all(self) -> list[AuditRecord]:
        """Read every record back, in the order it was written."""
        if self._db is None:
            raise RuntimeError("SqliteAuditSink.open() must be awaited before reading")
        cursor = await self._db.execute(
            """
            SELECT record_id, action, recorded_at, actor,
                   request_id, session_id, agent_id, project, detail
            FROM audit_log ORDER BY sequence
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            AuditRecord(
                record_id=row[0],
                action=AuditAction(row[1]),
                recorded_at=datetime.fromisoformat(row[2]),
                actor=row[3],
                request_id=row[4],
                session_id=row[5],
                agent_id=row[6],
                project=row[7],
                detail=json.loads(row[8]),
            )
            for row in rows
        ]


class AuditWriteError(Exception):
    """One or more sinks refused a record.

    The caller decides what to do, and for anything on the approval path the
    answer is to deny. A decision nobody can account for afterwards is not a
    decision worth acting on.
    """

    def __init__(self, record: AuditRecord, failures: Sequence[BaseException]) -> None:
        self.record = record
        self.failures = list(failures)
        super().__init__(
            f"{len(self.failures)} audit sink(s) failed for {record.action.value}: "
            + "; ".join(repr(f) for f in self.failures)
        )


class AuditLog:
    """Fans one record out to every sink."""

    def __init__(self, sinks: Iterable[AuditSink]) -> None:
        self._sinks = list(sinks)

    async def open(self) -> None:
        for sink in self._sinks:
            await sink.open()

    async def close(self) -> None:
        for sink in self._sinks:
            await sink.close()

    async def record(self, record: AuditRecord) -> AuditRecord:
        """Write to every sink, then raise if any of them refused.

        Every sink is attempted even after one fails. The sinks exist to be
        redundant, and abandoning the rest on the first error would throw away
        the redundancy exactly when it is needed.
        """
        failures: list[BaseException] = []
        for sink in self._sinks:
            try:
                await sink.write(record)
            except Exception as exc:  # every sink gets its turn, whatever went wrong
                failures.append(exc)
        if failures:
            raise AuditWriteError(record, failures)
        return record
