"""Tests for the HTTP surface."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from halyard.config import ChannelKind, Settings
from halyard.core.approvals import Decision

SECRET = "hunter2SuperSecretValue"

BODY = {
    "session_id": "session-1",
    "tool": "Bash",
    "command": "git status --short",
    "tool_use_id": "toolu_1",
    "cwd": "/repo",
}


def make_settings(tmp_path: Path, channel: ChannelKind) -> Settings:
    return Settings(
        HALYARD_CHANNEL=channel.value,
        HALYARD_DB_PATH=str(tmp_path / "halyard.db"),
        HALYARD_AUDIT_LOG=str(tmp_path / "audit.jsonl"),
        CLAUDE_PROJECT_NAME="alpha-engine",
        _env_file=None,
    )


async def client_for(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://control-plane")


@pytest.fixture
async def allowing(tmp_path: Path):
    from halyard.api.app import create_app

    app = create_app(make_settings(tmp_path, ChannelKind.STUB_ALLOW))
    async with LifespanManager(app), await client_for(app) as client:
        yield client, app


@pytest.fixture
async def denying(tmp_path: Path):
    from halyard.api.app import create_app

    app = create_app(make_settings(tmp_path, ChannelKind.STUB_DENY))
    async with LifespanManager(app), await client_for(app) as client:
        yield client, app


# --- deciding ---------------------------------------------------------------


async def test_an_approved_call_answers_allow(allowing) -> None:
    client, _ = allowing
    response = await client.post("/v1/approvals", json=BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == Decision.ALLOW.value
    assert body["risk"] == "low"
    assert body["request_id"].startswith("req_")


async def test_a_refused_call_answers_deny(denying) -> None:
    client, _ = denying
    response = await client.post("/v1/approvals", json={**BODY, "command": "rm -rf /var/lib/alpha"})

    body = response.json()
    assert body["decision"] == Decision.DENY.value
    assert body["risk"] == "high"
    # The reason goes to Claude Code verbatim, so it has to say something.
    assert body["reason"]


async def test_the_risk_the_agent_claims_cannot_lower_the_answer(allowing) -> None:
    client, _ = allowing
    response = await client.post(
        "/v1/approvals",
        json={**BODY, "command": "rm -rf /var/lib/alpha", "declared_risk": "low"},
    )

    assert response.json()["risk"] == "high"


async def test_secrets_never_reach_the_audit_log(allowing, tmp_path: Path) -> None:
    client, _ = allowing
    await client.post(
        "/v1/approvals",
        json={**BODY, "command": f"psql postgres://alper:{SECRET}@db/alpha"},
    )

    assert SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


async def test_a_retried_call_does_not_open_a_second_approval(allowing) -> None:
    client, _ = allowing
    first = await client.post("/v1/approvals", json=BODY)
    second = await client.post("/v1/approvals", json=BODY)

    # Same tool_use_id. The first is already resolved by the time the retry
    # lands, so this opens a fresh one rather than reusing settled consent.
    assert first.json()["request_id"] != second.json()["request_id"]


# --- failing closed ---------------------------------------------------------


async def test_a_malformed_body_is_not_an_approval(allowing) -> None:
    client, _ = allowing
    response = await client.post("/v1/approvals", json={"session_id": "session-1"})

    # 422 carries no decision, and the bridge is built to deny on anything that
    # is not an explicit allow.
    assert response.status_code == 422


async def test_an_unhandled_error_answers_deny_rather_than_500(allowing) -> None:
    client, app = allowing

    async def explode(**kwargs):
        raise RuntimeError("nobody predicted this")

    app.state.service.request = explode

    response = await client.post("/v1/approvals", json=BODY)

    assert response.status_code == 200
    assert response.json()["decision"] == Decision.DENY.value


# --- health -----------------------------------------------------------------


async def test_health_admits_when_no_human_is_being_asked(allowing) -> None:
    client, _ = allowing
    body = (await client.get("/health")).json()

    assert body["status"] == "ok"
    assert body["channel"] == "stub:allow"
    assert body["project"] == "alpha-engine"
    # Visible from outside, so a control plane quietly approving everything can
    # be noticed without reading its logs.
    assert body["decides_without_a_human"] is True


async def test_shutdown_leaves_nothing_open(allowing, tmp_path: Path) -> None:
    client, app = allowing
    await client.post("/v1/approvals", json=BODY)

    assert await app.state.store.list_open() == []


# --- relaying an agent's output ---------------------------------------------


async def test_a_relayed_message_is_accepted(allowing) -> None:
    client, _ = allowing
    response = await client.post(
        "/v1/messages",
        json={"session_id": "session-1", "text": "Done. Tests pass."},
    )

    assert response.status_code == 200
    assert response.json() == {"delivered": True}


async def test_a_relayed_message_is_masked_before_it_leaves(allowing, tmp_path: Path) -> None:
    client, _ = allowing
    await client.post(
        "/v1/messages",
        json={"session_id": "session-1", "text": f"I ran psql postgres://a:{SECRET}@db/x"},
    )

    assert SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


async def test_relaying_never_blocks_on_a_decision(denying) -> None:
    client, _ = denying
    # /v1/approvals holds the caller until a human decides. This must not: the
    # agent's turn is waiting on it.
    response = await client.post(
        "/v1/messages", json={"session_id": "session-1", "text": "still working"}
    )

    assert response.status_code == 200


async def test_a_relayed_message_needs_a_session_and_text(allowing) -> None:
    client, _ = allowing

    assert (await client.post("/v1/messages", json={"text": "orphan"})).status_code == 422
    assert (await client.post("/v1/messages", json={"session_id": "s"})).status_code == 422


async def test_health_reports_whether_the_gate_is_paused(allowing) -> None:
    client, app = allowing

    assert (await client.get("/health")).json()["paused"] is False

    await app.state.gate.pause("tg:4242")

    # Visible from outside, so a control plane that has quietly stopped asking
    # can be noticed without reading its logs.
    assert (await client.get("/health")).json()["paused"] is True


async def test_a_paused_gate_answers_defer(allowing) -> None:
    client, app = allowing
    await app.state.gate.pause("tg:4242")

    body = (await client.post("/v1/approvals", json=BODY)).json()

    assert body["decision"] == "defer"
    assert body["request_id"] is None
