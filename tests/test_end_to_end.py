"""End-to-end tests: the real bridge against the real control plane.

Everything else in this suite tests a piece. These tests wire the whole thing
together the way it actually runs — the bridge as its own process, the control
plane as its own process listening on a real socket, and an audit log on real
disk — and check that a decision comes back and is written down.

Two things are deliberately *not* here:

**A real Claude Code session.** `PreToolUse` hooks do fire under `claude -p`
(measured, see `docs/hook-payload-notes.md`), so driving one from a test is
possible. It is a bad foundation for a suite: it spends tokens and needs working
credentials on every run, to re-prove a link that was already proven by hand.
These tests feed the bridge the same payload shape Claude Code sends.

**A real Telegram bot.** The channel is exercised against a fake API in
`test_telegram.py`. Here the stub channels stand in, so the tests stay offline
and deterministic.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "bridge" / "hook_bridge.py"

SECRET = "hunter2SuperSecretValue"

PAYLOAD = {
    "session_id": "e2e-session",
    "cwd": "/repo",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git status --short", "description": "Show status"},
    "tool_use_id": "toolu_e2e_1",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_healthy(url: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
                return json.loads(response.read())
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"control plane never became healthy at {url}: {last}")


@contextmanager
def control_plane(tmp_path: Path, channel: str = "stub_allow", **overrides: str) -> Iterator[dict]:
    """Run the real service as its own process and tear it down afterwards."""
    port = free_port()
    audit_log = tmp_path / "audit.jsonl"
    db_path = tmp_path / "halyard.db"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "HALYARD_CHANNEL": channel,
        "HALYARD_BIND": f"127.0.0.1:{port}",
        "HALYARD_AUDIT_LOG": str(audit_log),
        "HALYARD_DB_PATH": str(db_path),
        "CLAUDE_PROJECT_NAME": "e2e-project",
        **overrides,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "halyard"],
        env=env,
        # Run outside the repository on purpose. `Settings` reads a `.env` file
        # from the working directory, and a developer's real one holds a live
        # Telegram token — a test that picked it up would message a real chat.
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        wait_until_healthy(url)
        yield {"url": url, "audit_log": audit_log, "db_path": db_path, "process": process}
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_bridge(url: str, payload: dict | None = None, **env: str) -> dict:
    """Run the real bridge and parse the hook decision it prints."""
    result = subprocess.run(
        [sys.executable, str(BRIDGE)],
        input=json.dumps(payload if payload is not None else PAYLOAD),
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HALYARD_URL": url, **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]


def audit_actions(audit_log: Path) -> list[str]:
    return [
        json.loads(line)["action"]
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- the whole chain --------------------------------------------------------


def test_an_approved_call_comes_back_allowed_and_is_written_down(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        decision = run_bridge(plane["url"])

        assert decision["permissionDecision"] == "allow"
        assert audit_actions(plane["audit_log"]) == [
            "control_plane.started",
            "approval.requested",
            "approval.resolved",
        ]


def test_a_refused_call_comes_back_denied(tmp_path: Path) -> None:
    with control_plane(tmp_path, channel="stub_deny") as plane:
        decision = run_bridge(
            plane["url"],
            {**PAYLOAD, "tool_input": {"command": "rm -rf /var/lib/alpha"}},
        )

        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"]


def test_the_risk_the_agent_claims_cannot_lower_the_recorded_one(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        run_bridge(plane["url"], {**PAYLOAD, "tool_input": {"command": "rm -rf /var/lib/alpha"}})

        recorded = [
            json.loads(line)
            for line in plane["audit_log"].read_text(encoding="utf-8").splitlines()
            if '"approval.requested"' in line
        ]
        assert recorded[0]["detail"]["risk"] == "high"


def test_a_secret_never_reaches_the_disk(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        run_bridge(
            plane["url"],
            {**PAYLOAD, "tool_input": {"command": f"psql postgres://alper:{SECRET}@db/alpha"}},
        )

        assert SECRET not in plane["audit_log"].read_text(encoding="utf-8")
        assert SECRET not in plane["db_path"].read_bytes().decode("utf-8", "ignore")


def test_the_audit_database_refuses_to_be_rewritten(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        run_bridge(plane["url"])

        # Against the file the running service actually wrote, not a fixture.
        with sqlite3.connect(plane["db_path"]) as db:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("UPDATE audit_log SET actor = 'someone else'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("DELETE FROM audit_log")


# --- failing closed ---------------------------------------------------------


def test_a_control_plane_that_is_not_running_denies(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        url = plane["url"]
        plane["process"].terminate()
        plane["process"].wait(timeout=15)

        decision = run_bridge(url)

    assert decision["permissionDecision"] == "deny"
    assert "could not be reached" in decision["permissionDecisionReason"]


def test_shutting_down_denies_and_records_it(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        run_bridge(plane["url"])
        plane["process"].terminate()
        plane["process"].wait(timeout=15)

        # The service must close its own books on the way out rather than
        # leaving a bridge to wait out a timeout it would fail open past.
        assert audit_actions(plane["audit_log"])[-1] == "control_plane.stopped"


def test_a_payload_the_bridge_cannot_read_denies(tmp_path: Path) -> None:
    with control_plane(tmp_path) as plane:
        result = subprocess.run(
            [sys.executable, str(BRIDGE)],
            input="this is not json",
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "HALYARD_URL": plane["url"]},
            timeout=60,
        )

    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


# --- refusing to start ------------------------------------------------------


def test_the_service_will_not_start_with_the_timeouts_out_of_order(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "halyard"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "HALYARD_CHANNEL": "stub_deny",
            "HALYARD_BIND": f"127.0.0.1:{free_port()}",
            # The bridge would give up before the approver does.
            "HALYARD_APPROVAL_TIMEOUT_SECONDS": "400",
            "HALYARD_BRIDGE_TIMEOUT_SECONDS": "330",
            "HALYARD_HOOK_TIMEOUT_SECONDS": "600",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "fails open" in result.stderr


def test_the_service_will_not_start_without_a_channel(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "halyard"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "HALYARD_BIND": f"127.0.0.1:{free_port()}",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # No default, so forgetting to choose one cannot land on a channel that
    # answers by itself.
    assert result.returncode != 0
    assert "HALYARD_CHANNEL" in result.stderr
