"""An inspection's trace: what it read, from the runtime's stream and the gate.

The streams here are in the shapes measured on 2026-10-25 — `codex exec --json`
on Codex 0.160.0 and `claude -p --output-format stream-json --verbose` on
Claude Code 2.1.303 — and go through the runners' own readers, so what is
merged is what a turn would have reported.
"""

from __future__ import annotations

from pathlib import Path

from halyard import inspections
from halyard.agents.claude_code.runner import heard
from halyard.agents.codex.runner import ran
from halyard.core.calls import Calls
from halyard.inspections import Decided, Outcome, Read, Status, Trace
from halyard.inspections import trace as tracing


def codex(*events: tuple[str, str, str, int | None]) -> Calls:
    """Calls as Codex reports them: (event, item id, command, exit code)."""
    calls = Calls()
    calls.reported = True
    for kind, ident, command, code in events:
        item = {
            "id": ident,
            "type": "command_execution",
            "command": f"/bin/zsh -lc '{command}'",
            "aggregated_output": "",
            "exit_code": code,
            "status": "in_progress" if kind == "item.started" else "completed",
        }
        ran(calls, kind, item)
    return calls


def claude(*events: dict) -> Calls:
    calls = Calls()
    calls.reported = True
    for event in events:
        heard(calls, event)
    return calls


def called(*uses: tuple[str, str, dict]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": ident, "name": name, "input": given}
                for ident, name, given in uses
            ]
        },
    }


def came_back(*results: tuple[str, bool, str]) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": ident, "is_error": error, "content": said}
                for ident, error, said in results
            ]
        },
    }


def answered(trace: Trace) -> inspections.Answer:
    return inspections.Answer(
        "claims", Path("NOTES/claims.md"), "3e8c847", "no finding", trace=trace
    )


def test_a_codex_read_the_hook_denied_is_in_the_trace_from_its_card() -> None:
    """Codex's stream says what ran and nothing else: a read the hook denied,
    or one still waiting on it, comes from the gate. One the gate allowed is
    the stream's, said once."""
    calls = codex(
        ("item.started", "item_0", "cat other.txt", None),
        ("item.completed", "item_0", "cat other.txt", 0),
        ("item.started", "item_1", "rg -n seed src", None),
        ("item.completed", "item_1", "rg -n seed src", 1),
    )
    gate = [
        Decided("call_a", "Bash", "cat other.txt", None, "allowed by tg:4242"),
        Decided("call_b", "Bash", "cat /tmp/x.log", Outcome.DENIED, "denied by tg:4242"),
        Decided(
            "call_c",
            "Bash",
            "tail -5 run.log",
            Outcome.ENDED,
            "still waiting for approval when the inspection ended",
        ),
    ]

    trace = inspections.merged(calls, gate, reported=calls.reported)

    assert trace.reads == (
        Read("Bash", "cat other.txt", Outcome.RAN),
        Read("Bash", "rg -n seed src", Outcome.RAN, "exit 1"),
        Read("Bash", "cat /tmp/x.log", Outcome.DENIED, "denied by tg:4242"),
        Read(
            "Bash",
            "tail -5 run.log",
            Outcome.ENDED,
            "still waiting for approval when the inspection ended",
        ),
    )
    assert [read.ran for read in trace.reads] == [True, True, False, False]
    answer = answered(trace)
    assert answer.status is Status.INCOMPLETE
    assert inspections.said(answer) == "incomplete · 4 reads, 2 did not run"


def test_a_claude_read_and_its_card_are_one_read() -> None:
    """Claude Code's hook carries the id its stream does, so the gate says why
    a read it stopped never ran. A tool's own error ran; a call with no result
    and no card was cut off."""
    calls = claude(
        called(
            ("toolu_1", "Read", {"file_path": "src/loader.py"}),
            ("toolu_2", "Bash", {"command": "git log -3"}),
            ("toolu_3", "Bash", {"command": "cat /tmp/x.log"}),
            ("toolu_4", "Read", {"file_path": "src/gone.py"}),
        ),
        came_back(
            ("toolu_1", False, "def seed(): ..."),
            ("toolu_2", False, "3e8c847 …"),
            ("toolu_3", True, "PreToolUse:Bash hook error: Denied by tg:4242"),
            ("toolu_4", True, "File does not exist."),
        ),
        called(
            ("toolu_5", "Bash", {"command": "tail -5 run.log"}),
            ("toolu_6", "Grep", {"pattern": "seed"}),
        ),
    )
    gate = [
        Decided("toolu_2", "Bash", "git log -3", None, "pre-authorized (reads)"),
        Decided("toolu_3", "Bash", "cat /tmp/x.log", Outcome.DENIED, "denied by tg:4242"),
        Decided(
            "toolu_5",
            "Bash",
            "tail -5 run.log",
            Outcome.ENDED,
            "still waiting for approval when the inspection ended",
        ),
    ]

    trace = inspections.merged(calls, gate, reported=True)

    assert trace.reads == (
        Read("Read", "src/loader.py", Outcome.RAN),
        Read("Bash", "git log -3", Outcome.RAN, "pre-authorized (reads)"),
        Read("Bash", "cat /tmp/x.log", Outcome.DENIED, "denied by tg:4242"),
        Read("Read", "src/gone.py", Outcome.ERROR),
        Read(
            "Bash",
            "tail -5 run.log",
            Outcome.ENDED,
            "still waiting for approval when the inspection ended",
        ),
        Read(
            "Grep",
            "seed",
            Outcome.UNFINISHED,
            "started, and had not come back when the inspection ended",
        ),
    )
    assert [n.command for n in trace.not_run] == ["cat /tmp/x.log", "tail -5 run.log", "seed"]


def test_a_read_the_hook_refused_with_no_card_is_still_denied() -> None:
    calls = claude(
        called(("toolu_1", "Bash", {"command": "git commit -m x"})),
        came_back(("toolu_1", True, "PreToolUse:Bash hook error: Refused")),
    )

    [read] = inspections.merged(calls, [], reported=True).reads

    assert (read.outcome, read.detail) == (Outcome.DENIED, "denied by the hook")


def test_where_the_runtime_says_nothing_the_gate_s_reads_are_what_is_known() -> None:
    """opencode: a read the gate allowed counts as run, and the count says
    whose it is."""
    gate = [
        Decided("", "bash", "rg seed", None, "pre-authorized (reads)"),
        Decided("", "bash", "cat /tmp/x.log", Outcome.EXPIRED, "expired before anybody answered"),
    ]

    trace = inspections.merged(Calls(), gate, reported=False)

    assert [read.outcome for read in trace.reads] == [Outcome.ALLOWED, Outcome.EXPIRED]
    assert trace.reads[0].ran
    assert (
        inspections.said(answered(trace)) == "incomplete · 2 reads seen at the gate, 1 did not run"
    )


def test_each_status_in_a_few_words() -> None:
    every = Trace((Read("Read", "a.py", Outcome.RAN), Read("Bash", "rg x", Outcome.RAN)), True)
    one = Trace((Read("Read", "a.py", Outcome.RAN),), True)
    unanswered = inspections.Answer("claims", Path("c.md"), "v", why="the model did not answer")

    assert answered(every).status is Status.COMPLETE
    assert inspections.said(answered(every)) == "complete · 2 reads"
    assert inspections.said(answered(one)) == "complete · 1 read"
    assert unanswered.status is Status.UNMEASURED
    assert inspections.said(unanswered) == "unmeasured — the model did not answer"
    assert inspections.said(unanswered, why=False) == "unmeasured"


def test_a_trace_is_kept_as_json_and_read_back_whole() -> None:
    trace = Trace(
        (
            Read("Bash", "rg -n seed src", Outcome.RAN, "exit 1"),
            Read("Bash", "cat /tmp/x.log", Outcome.DENIED, "denied by tg:4242"),
        ),
        reported=True,
    )

    assert tracing.from_json(tracing.to_json(trace)) == trace
    assert tracing.from_json(None) is None
    assert tracing.from_json("not json") is None
    unknown = '{"reported": true, "reads": [{"tool": "Bash", "what": "x", "outcome": "?"}]}'
    assert tracing.from_json(unknown) == Trace((), reported=True)


def test_what_a_call_read_is_redacted_as_a_card_is() -> None:
    """A command from the stream is kept, and shown, as a card would show it."""
    calls = codex(
        ("item.started", "item_0", "psql postgres://alper:hunter2@db.internal:5432/alpha", None)
    )

    [call] = list(calls)

    assert "hunter2" not in call.what
    assert call.what.startswith("psql postgres://")
