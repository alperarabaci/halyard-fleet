"""Tests for the end-to-end approval path.

Mostly about what happens when something breaks. The happy path is one test;
the rest are the ways this could quietly start approving things.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from halyard.channels.stub import StubChannel
from halyard.core.approvals import ApprovalRequest, ApprovalStore, Decision
from halyard.core.audit import AuditAction, AuditLog, AuditRecord, JsonlAuditSink
from halyard.core.events import RiskLevel
from halyard.core.policy import Policy
from halyard.core.redaction import Redactor
from halyard.core.registry import SessionRegistry
from halyard.core.service import ApprovalService, BridgeDecision

SECRET = "hunter2SuperSecretValue"


class BrokenSink:
    """An audit sink that never accepts anything."""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, record: AuditRecord) -> None:
        raise OSError("disk full")


class ExplodingChannel:
    """A channel that cannot deliver."""

    name = "exploding"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_approval_request(self, request: ApprovalRequest) -> str:
        raise ConnectionError("telegram unreachable")

    async def send_message(
        self, session_id: str, text: str, role=None, *, agent_id=None, session_name=None
    ) -> str: ...
    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str: ...


class SilentChannel:
    """A channel that accepts the request and then never answers."""

    name = "silent"
    last_request: ApprovalRequest | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_approval_request(self, request: ApprovalRequest) -> str:
        self.last_request = request
        return "sent"

    async def send_message(
        self, session_id: str, text: str, role=None, *, agent_id=None, session_name=None
    ) -> str: ...
    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role=None,
        *,
        agent_id=None,
        session_name=None,
    ) -> str: ...


def build_service(
    tmp_path: Path,
    *,
    channel=None,
    store: ApprovalStore | None = None,
    audit: AuditLog | None = None,
    ttl: timedelta = timedelta(minutes=5),
    gate: Gate | None = None,
    refuse_agent_commits: bool = False,
) -> tuple[ApprovalService, ApprovalStore, JsonlAuditSink]:
    store = store or ApprovalStore(ttl=ttl)
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = audit or AuditLog([sink])
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=audit,
        registry=SessionRegistry(),
        channel=channel if channel is not None else StubChannel(store, Decision.ALLOW),
        project="alpha-engine",
        **({"gate": gate} if gate is not None else {}),
        refuse_agent_commits=refuse_agent_commits,
    )
    return service, store, sink


async def ask(service: ApprovalService, command: str = "git status", **kwargs):
    defaults = {"session_id": "session-1", "agent_id": "claude-code", "tool": "Bash"}
    return await service.request(command=command, **{**defaults, **kwargs})


# --- the path works ---------------------------------------------------------


async def test_an_approved_request_comes_back_allowed(tmp_path: Path) -> None:
    service, _, sink = build_service(tmp_path)
    await sink.open()

    outcome = await ask(service, "git status")

    assert outcome.allowed
    assert outcome.request_id is not None
    assert outcome.risk is RiskLevel.LOW
    assert [r.action for r in await sink.read_all()] == [
        AuditAction.APPROVAL_REQUESTED,
        AuditAction.APPROVAL_RESOLVED,
    ]


async def test_a_refused_request_comes_back_denied(tmp_path: Path) -> None:
    store = ApprovalStore()
    service, _, sink = build_service(
        tmp_path, store=store, channel=StubChannel(store, Decision.DENY)
    )
    await sink.open()

    outcome = await ask(service, "rm -rf build")

    assert not outcome.allowed
    assert outcome.risk is RiskLevel.HIGH


async def test_the_command_is_classified_before_anyone_sees_it(tmp_path: Path) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "docker compose down postgres")

    assert channel.last_request is not None
    assert channel.last_request.risk is RiskLevel.HIGH


async def test_secrets_are_masked_before_the_channel_or_the_log_see_them(
    tmp_path: Path,
) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, f"psql postgres://alper:{SECRET}@db/alpha")

    assert channel.last_request is not None
    assert SECRET not in channel.last_request.command_full
    assert SECRET not in channel.last_request.command_summary
    assert all(SECRET not in str(r.detail) for r in await sink.read_all())


async def test_the_request_is_recorded_before_it_is_delivered(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()
    seen: list[int] = []

    class CheckingChannel(SilentChannel):
        async def send_approval_request(self, request: ApprovalRequest) -> str:
            seen.append(len(await sink.read_all()))
            return await super().send_approval_request(request)

    store = ApprovalStore(ttl=timedelta(milliseconds=50))
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=CheckingChannel(),
        project="alpha-engine",
    )

    await ask(service, "git status")

    # An approval that reached a human before it reached the log is one that
    # could be acted on with no record that it was ever asked.
    assert seen == [1]


# --- the path breaks --------------------------------------------------------


async def test_nobody_answering_denies(tmp_path: Path) -> None:
    service, _, sink = build_service(
        tmp_path, channel=SilentChannel(), ttl=timedelta(milliseconds=50)
    )
    await sink.open()

    outcome = await ask(service)

    assert not outcome.allowed
    assert "expired" in outcome.reason.lower()


async def test_an_undeliverable_request_denies(tmp_path: Path) -> None:
    service, store, sink = build_service(tmp_path, channel=ExplodingChannel())
    await sink.open()

    outcome = await ask(service)

    assert not outcome.allowed
    assert "deliver" in outcome.reason.lower()
    # And it is closed out, not left open to be answered by somebody later.
    assert await store.list_open() == []


async def test_an_unwritable_audit_log_denies(tmp_path: Path) -> None:
    service, _, _ = build_service(tmp_path, audit=AuditLog([BrokenSink()]))

    outcome = await ask(service, "git status")

    # A decision nobody can account for afterwards is not one to act on.
    assert not outcome.allowed
    assert "audit" in outcome.reason.lower()


async def test_an_approval_that_cannot_be_recorded_is_not_honoured(tmp_path: Path) -> None:
    """The audit log works for the request and fails for the decision."""
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    await sink.open()

    class FailsOnResolution:
        async def open(self) -> None: ...
        async def close(self) -> None: ...

        async def write(self, record: AuditRecord) -> None:
            if record.action is AuditAction.APPROVAL_RESOLVED:
                raise OSError("disk full")
            await sink.write(record)

    store = ApprovalStore()
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([FailsOnResolution()]),
        registry=SessionRegistry(),
        channel=StubChannel(store, Decision.ALLOW),
        project="alpha-engine",
    )

    outcome = await ask(service, "git status")

    # An unrecorded denial is still a denial and stands. An unrecorded approval
    # is a command about to run with no trace of who agreed to it.
    assert not outcome.allowed
    assert "audit" in outcome.reason.lower()


async def test_an_unexpected_failure_denies_instead_of_raising(tmp_path: Path) -> None:
    class ExplodingPolicy(Policy):
        def classify(self, command: str, *, declared=None):
            raise RuntimeError("something nobody predicted")

    service, _, sink = build_service(tmp_path)
    service._policy = ExplodingPolicy()
    await sink.open()

    outcome = await ask(service)

    # request() must never raise. An exception escaping it becomes a 500, and a
    # hook that gets a 500 without a decision runs the command.
    assert not outcome.allowed
    assert "internal error" in outcome.reason.lower()


# --- naming the project a request came from -----------------------------------


@pytest.mark.parametrize(
    ("project_dir", "cwd", "expected"),
    [
        ("/Users/j/dev/agent-platform", "/Users/j/dev/agent-platform/sub", "agent-platform"),
        (None, "/Users/j/dev/agent-platform", "agent-platform"),
        ("/Users/j/dev/halyard-fleet/", None, "halyard-fleet"),
        # Nothing to go on, so the configured name is all that is left.
        (None, None, "configured-name"),
        ("", "", "configured-name"),
    ],
)
def test_the_project_is_named_from_the_path_it_came_from(
    project_dir: str | None, cwd: str | None, expected: str
) -> None:
    from halyard.core.service import project_name

    # CLAUDE_PROJECT_NAME is one value in one control plane. Gate a second
    # repository and its approvals would arrive wearing the first one's name —
    # found in real use, with a command from agent-platform arriving as
    # alpha-engine.
    assert project_name(project_dir, cwd, "configured-name") == expected


async def test_an_approval_card_names_the_calling_project(tmp_path: Path) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "ls", project_dir="/Users/j/dev/agent-platform")

    assert channel.last_request is not None
    # The service was configured with "alpha-engine".
    assert channel.last_request.project == "agent-platform"


async def test_a_request_without_a_project_dir_keeps_the_configured_name(
    tmp_path: Path,
) -> None:
    channel = SilentChannel()
    service, _, sink = build_service(tmp_path, channel=channel, ttl=timedelta(milliseconds=50))
    await sink.open()

    await ask(service, "ls")

    assert channel.last_request is not None
    assert channel.last_request.project == "alpha-engine"


# --- questions: the fail-open twin of the approval path ----------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from halyard.core.gate import Gate  # noqa: E402
from halyard.core.questions import Choice, QuestionStore  # noqa: E402
from halyard.core.service import QuestionService  # noqa: E402


class QuestionChannel:
    """Captures the question and lets a test answer it out of band."""

    name = "questions"

    def __init__(self, store: QuestionStore) -> None:
        self._store = store
        self.last = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_question(self, request) -> str:
        self.last = request
        return "sent"


class ExplodingQuestionChannel:
    name = "boom"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_question(self, request) -> str:
        raise ConnectionError("telegram unreachable")


def build_questions(tmp_path: Path, *, channel, gate=None, store=None):
    store = store or QuestionStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    audit = AuditLog([sink])
    service = QuestionService(
        store=store,
        audit=audit,
        registry=SessionRegistry(),
        channel=channel,
        project="alpha-engine",
        gate=gate or Gate(),
    )
    return service, store, sink


async def ask_question(service: QuestionService, **kwargs):
    defaults = {
        "session_id": "session-1",
        "agent_id": "claude-code",
        "question": "Which color?",
        "options": [Choice(label="Red"), Choice(label="Blue")],
    }
    return await service.ask(**{**defaults, **kwargs})


async def test_a_chosen_answer_comes_back(tmp_path: Path) -> None:
    store = QuestionStore(ttl=timedelta(minutes=5))
    channel = QuestionChannel(store)
    service, store, sink = build_questions(tmp_path, channel=channel, store=store)
    await sink.open()

    async def answer_it() -> None:
        while channel.last is None:
            await asyncio.sleep(0)
        await store.answer(channel.last.request_id, nonce=channel.last.nonce, answer="Red")

    task = asyncio.create_task(answer_it())
    outcome = await ask_question(service)
    await task

    assert outcome.answer == "Red"
    actions = [r.action for r in await sink.read_all()]
    assert AuditAction.QUESTION_ASKED in actions
    assert AuditAction.QUESTION_ANSWERED in actions


async def test_a_paused_gate_leaves_the_question_to_the_terminal(tmp_path: Path) -> None:
    gate = Gate()
    await gate.pause("tester")
    store = QuestionStore(ttl=timedelta(minutes=5))
    channel = QuestionChannel(store)
    service, _, sink = build_questions(tmp_path, channel=channel, gate=gate, store=store)
    await sink.open()

    outcome = await ask_question(service)

    # Pausing means the phone is off; the choice belongs at the desk.
    assert outcome.answer is None
    assert channel.last is None


async def test_a_channel_that_cannot_deliver_falls_back_to_the_terminal(tmp_path: Path) -> None:
    service, _, sink = build_questions(tmp_path, channel=ExplodingQuestionChannel())
    await sink.open()

    outcome = await ask_question(service)

    # The approval twin denies here. A question is not dangerous to leave to the
    # desk, so it says nothing instead.
    assert outcome.answer is None


async def test_a_question_nobody_answers_comes_back_empty(tmp_path: Path) -> None:
    store = QuestionStore(ttl=timedelta(seconds=0))
    channel = QuestionChannel(store)
    service, _, sink = build_questions(tmp_path, channel=channel, store=store)
    await sink.open()

    outcome = await ask_question(service)

    assert outcome.answer is None


# --- the one grant: writes to a configured path ------------------------------


def build_with_writes(tmp_path: Path, patterns: tuple[str, ...], *, channel=None):
    """A service that would otherwise deny everything, so a grant is visible."""
    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=channel if channel is not None else StubChannel(store, Decision.DENY),
        project="alpha-engine",
        allowed_writes=patterns,
    )
    return service, sink


async def test_a_write_to_a_configured_path_is_allowed_without_a_card(tmp_path: Path) -> None:
    """The channel here denies everything, so an allow can only have come from
    the configuration rather than from anybody being asked."""
    project = tmp_path / "repo"
    (project / "NOTES").mkdir(parents=True)
    service, sink = build_with_writes(tmp_path, ("NOTES/**",))
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="Write",
        command="Write NOTES/p2.md",
        project_dir=str(project),
        file_path=str(project / "NOTES" / "p2.md"),
    )

    assert outcome.allowed
    assert "NOTES/**" in outcome.reason


async def test_the_grant_is_written_down_with_the_pattern(tmp_path: Path) -> None:
    """ "Why did that run without asking" must always have an answer."""
    project = tmp_path / "repo"
    (project / "NOTES").mkdir(parents=True)
    service, sink = build_with_writes(tmp_path, ("NOTES/**",))
    await sink.open()

    await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="Write",
        command="Write NOTES/p2.md",
        project_dir=str(project),
        file_path=str(project / "NOTES" / "p2.md"),
    )

    records = await sink.read_all()
    assert [r.action for r in records] == [AuditAction.WRITE_PREAUTHORIZED]
    assert records[0].detail["pattern"] == "NOTES/**"
    assert records[0].actor == "config"


async def test_a_write_outside_the_configured_paths_still_asks(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    service, sink = build_with_writes(tmp_path, ("NOTES/**",))
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="Write",
        command="Write src/main.py",
        project_dir=str(project),
        file_path=str(project / "src" / "main.py"),
    )

    # It reached the channel — which denies here — rather than being granted.
    assert not outcome.allowed
    assert AuditAction.APPROVAL_REQUESTED in {r.action for r in await sink.read_all()}


async def test_a_bash_command_is_never_granted_by_a_path(tmp_path: Path) -> None:
    """Its whole argument is a command, not a destination. A `writes:` entry
    must not become a way to run things without asking."""
    project = tmp_path / "repo"
    (project / "NOTES").mkdir(parents=True)
    service, sink = build_with_writes(tmp_path, ("NOTES/**", "**"))
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="Bash",
        command="rm -rf NOTES",
        project_dir=str(project),
        file_path=str(project / "NOTES" / "x.md"),
    )

    assert not outcome.allowed


async def test_a_paused_gate_still_defers_rather_than_granting(tmp_path: Path) -> None:
    """Pausing hands the question back to the runtime. A configured path must
    not turn that into an approval Halyard made while stepped aside."""
    from halyard.core.gate import Gate

    project = tmp_path / "repo"
    (project / "NOTES").mkdir(parents=True)
    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    gate = Gate()
    await gate.pause("tester")
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=StubChannel(store, Decision.DENY),
        project="alpha-engine",
        gate=gate,
        allowed_writes=("NOTES/**",),
    )
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="Write",
        command="Write NOTES/p2.md",
        project_dir=str(project),
        file_path=str(project / "NOTES" / "p2.md"),
    )

    assert outcome.decision is BridgeDecision.DEFER


async def test_a_named_tool_runs_without_a_card(tmp_path: Path) -> None:
    """The channel denies everything here, so an allow can only have come from
    the configuration."""
    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=StubChannel(store, Decision.DENY),
        project="alpha-engine",
        allowed_tools=("mcp__*__list_*",),
    )
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="mcp__claude_ai_alpha_explore_prod__list_companies",
        command="{}",
    )

    assert outcome.allowed
    records = await sink.read_all()
    assert [r.action for r in records] == [AuditAction.TOOL_PREAUTHORIZED]
    assert records[0].detail["pattern"] == "mcp__*__list_*"


async def test_an_unnamed_tool_still_asks(tmp_path: Path) -> None:
    store = ApprovalStore(ttl=timedelta(minutes=5))
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store=store,
        policy=Policy(),
        redactor=Redactor(),
        audit=AuditLog([sink]),
        registry=SessionRegistry(),
        channel=StubChannel(store, Decision.DENY),
        project="alpha-engine",
        allowed_tools=("mcp__*__list_*",),
    )
    await sink.open()

    outcome = await service.request(
        session_id="s1",
        agent_id="claude-code",
        tool="mcp__claude_ai_alpha_explore_prod__propose_prompt",
        command="{}",
    )

    assert not outcome.allowed
    assert AuditAction.APPROVAL_REQUESTED in {r.action for r in await sink.read_all()}


# --- what is refused before anybody is asked --------------------------------


async def test_an_agents_commit_is_refused_when_that_is_switched_on(tmp_path: Path) -> None:
    """Halyard commits on request from a phone, with the diff summarised and a
    message to approve. An agent deciding on its own that now is the moment to
    write history is a different act."""
    service, store, sink = build_service(tmp_path, refuse_agent_commits=True)
    await sink.open()

    outcome = await ask(service, "git commit -m 'wip'")

    assert outcome.decision is BridgeDecision.DENY
    assert "agents do not commit" in outcome.reason
    # Nothing was asked: no card, no request anybody has to answer.
    assert await store.list_open() == []
    await sink.close()


async def test_it_is_off_unless_somebody_asks_for_it(tmp_path: Path) -> None:
    """Nothing changes for anybody who has not switched it on."""
    service, _, sink = build_service(tmp_path)
    await sink.open()

    assert (await ask(service, "git commit -m 'wip'")).decision is BridgeDecision.ALLOW
    await sink.close()


async def test_a_pause_does_not_lift_it(tmp_path: Path) -> None:
    """Pausing means "stop asking me", and hands each call back to the runtime's
    own permission list — the right answer for a question and the wrong one for
    a rule. A guard a pause switches off is a guard nobody can rely on."""
    gate = Gate()
    service, _, sink = build_service(tmp_path, gate=gate, refuse_agent_commits=True)
    await sink.open()
    await gate.pause("tg:1")

    outcome = await ask(service, "git push")

    assert outcome.decision is BridgeDecision.DENY
    # And an ordinary command still defers, so pause otherwise means what it did.
    assert (await ask(service, "git status")).decision is BridgeDecision.DEFER
    await sink.close()


async def test_it_cannot_be_configured_around(tmp_path: Path) -> None:
    """Checked before the allow-lists, so naming `Bash` under `tools:` cannot
    turn a refusal into a grant."""
    service, _, sink = build_service(tmp_path, refuse_agent_commits=True)
    service._tools = ("Bash",)
    await sink.open()

    assert (await ask(service, "git push")).decision is BridgeDecision.DENY
    await sink.close()


async def test_a_refusal_is_recorded_as_its_own_thing(tmp_path: Path) -> None:
    """Not as a denial: a denial is what a person chose, and counting the two
    together would make a Tuesday look like refusals somebody never made."""
    service, _, sink = build_service(tmp_path, refuse_agent_commits=True)
    await sink.open()

    await ask(service, "git commit -m x")
    await sink.close()

    written = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    refused = [one for one in written if one["action"] == "call.refused"]
    assert len(refused) == 1
    assert refused[0]["detail"]["act"] == "commit"
    assert refused[0]["actor"] == "config"
