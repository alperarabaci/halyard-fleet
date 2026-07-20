"""Tests for the hook bridge.

Run as real subprocesses, piping JSON in and reading stdout back, because what
is being tested is process-level behaviour: what gets printed, and what the exit
code is. Importing the module would test neither.

Every test asserts the exit code is 0. A bridge that exits 2 blocks the call
too, but Claude Code frames that as `PreToolUse:Bash hook error:` rather than as
a decision — the agent should be told it was denied, not that the plumbing broke.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "bridge" / "hook_bridge.py"
WRAPPER = REPO / "bridge" / "hook.sh"

PAYLOAD = {
    "session_id": "session-1",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/repo",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "docker compose down", "description": "Stop the stack"},
    "tool_use_id": "toolu_1",
}


@contextmanager
def control_plane(
    *, status: int = 200, body: dict | None = None, raw: bytes | None = None, delay: float = 0.0
) -> Iterator[tuple[str, list[dict]]]:
    """A stand-in control plane that answers however a test needs it to."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            if delay:
                time.sleep(delay)
            payload = raw if raw is not None else json.dumps(body or {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()


def run(script: Path, payload: object = PAYLOAD, **env: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(script)] if script.suffix == ".py" else ["/bin/sh", str(script)]
    return subprocess.run(
        command,
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
        timeout=30,
    )


def decision_of(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse what the hook printed, the way Claude Code would."""
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    return output["hookSpecificOutput"]


# --- the path works ---------------------------------------------------------


def test_an_allowed_request_prints_allow() -> None:
    with control_plane(body={"decision": "allow", "reason": "Allowed by tg:4242."}) as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url))

    assert decision["permissionDecision"] == "allow"
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecisionReason"] == "Allowed by tg:4242."


def test_a_denied_request_prints_deny_with_the_reason_it_was_given() -> None:
    with control_plane(body={"decision": "deny", "reason": "Denied by tg:4242."}) as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url))

    assert decision["permissionDecision"] == "deny"
    # Claude Code hands this to the model verbatim, so the human's reason has to
    # survive the trip rather than being replaced by a generic message.
    assert decision["permissionDecisionReason"] == "Denied by tg:4242."


def test_the_payload_is_forwarded_without_being_reinterpreted() -> None:
    with control_plane(body={"decision": "allow", "reason": "ok"}) as (url, received):
        run(BRIDGE, HALYARD_URL=url)

    assert received[0] == {
        "session_id": "session-1",
        "agent_id": "claude-code",
        "tool": "Bash",
        "command": "docker compose down",
        "tool_use_id": "toolu_1",
        "cwd": "/repo",
    }


def test_a_tool_without_a_command_is_described_rather_than_dropped() -> None:
    payload = {**PAYLOAD, "tool_name": "Write", "tool_input": {"file_path": "/a", "content": "b"}}
    with control_plane(body={"decision": "allow", "reason": "ok"}) as (url, received):
        run(BRIDGE, payload, HALYARD_URL=url)

    assert json.loads(received[0]["command"]) == {"file_path": "/a", "content": "b"}


# --- everything that can go wrong -------------------------------------------


def test_an_unreachable_control_plane_denies() -> None:
    # Port 1 is not going to be listening.
    decision = decision_of(run(BRIDGE, HALYARD_URL="http://127.0.0.1:1"))

    assert decision["permissionDecision"] == "deny"
    assert "could not be reached" in decision["permissionDecisionReason"]


def test_a_server_error_denies() -> None:
    with control_plane(status=500, body={"detail": "boom"}) as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url))

    assert decision["permissionDecision"] == "deny"


def test_an_unparseable_answer_denies() -> None:
    with control_plane(raw=b"<html>not json</html>") as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url))

    assert decision["permissionDecision"] == "deny"


def test_a_slow_control_plane_denies() -> None:
    with control_plane(body={"decision": "allow", "reason": "too late"}, delay=1.5) as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url, HALYARD_BRIDGE_TIMEOUT_SECONDS="0.3"))

    assert decision["permissionDecision"] == "deny"
    assert "did not answer" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"decision": None},
        {"decision": "ALLOW"},
        {"decision": "maybe"},
        {"decision": "allowed"},
        {"reason": "looks fine to me"},
    ],
)
def test_anything_that_is_not_exactly_allow_denies(answer: dict) -> None:
    with control_plane(body=answer) as (url, _):
        decision = decision_of(run(BRIDGE, HALYARD_URL=url))

    # A missing field, a typo, a casing difference, a decision this bridge has
    # never heard of. There is no benefit of the doubt to give here.
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize("stdin", ["", "not json at all", "[]", "null", '{"broken":'])
def test_an_unreadable_payload_denies(stdin: str) -> None:
    decision = decision_of(run(BRIDGE, stdin, HALYARD_URL="http://127.0.0.1:1"))

    assert decision["permissionDecision"] == "deny"


def test_a_denial_says_who_denied_it() -> None:
    decision = decision_of(run(BRIDGE, HALYARD_URL="http://127.0.0.1:1"))

    # The agent should be able to tell a Halyard denial from a Claude Code one.
    assert decision["permissionDecisionReason"].startswith("Denied by Halyard:")


# --- the wrapper, for failures the bridge never gets to handle ---------------


def test_the_wrapper_passes_a_real_decision_through() -> None:
    with control_plane(body={"decision": "allow", "reason": "ok"}) as (url, _):
        decision = decision_of(run(WRAPPER, HALYARD_URL=url))

    assert decision["permissionDecision"] == "allow"


def test_the_wrapper_denies_when_there_is_no_interpreter() -> None:
    decision = decision_of(run(WRAPPER, HALYARD_PYTHON="/nonexistent/python"))

    assert decision["permissionDecision"] == "deny"
    assert "could not run" in decision["permissionDecisionReason"]


def fake_python(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-python"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return str(path)


def test_the_wrapper_denies_when_the_bridge_prints_nonsense(tmp_path: Path) -> None:
    interpreter = fake_python(tmp_path, "echo 'Traceback (most recent call last):'; exit 1")

    decision = decision_of(run(WRAPPER, HALYARD_PYTHON=interpreter))

    # This is the case that matters: a crash before the bridge can print
    # anything exits non-zero with a traceback, which Claude Code reads as no
    # opinion and runs the command.
    assert decision["permissionDecision"] == "deny"


def test_the_wrapper_denies_a_decision_that_came_with_a_bad_exit_code(tmp_path: Path) -> None:
    allow = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    interpreter = fake_python(tmp_path, f"echo '{allow}'; exit 1")

    decision = decision_of(run(WRAPPER, HALYARD_PYTHON=interpreter))

    # Well-formed output from a process that failed is not a decision to trust.
    assert decision["permissionDecision"] == "deny"


def test_the_wrapper_denies_when_the_bridge_prints_nothing(tmp_path: Path) -> None:
    interpreter = fake_python(tmp_path, "exit 0")

    decision = decision_of(run(WRAPPER, HALYARD_PYTHON=interpreter))

    # Empty stdout with a clean exit is how Claude Code spells "no opinion".
    assert decision["permissionDecision"] == "deny"
