"""Tests for putting a message into a ZCode session — the account it is shown,
the questions it asks, and what Halyard answers.

Against a stand-in engine that speaks the protocol the real one was measured
speaking: it asks for preferences, for the credential, and for permission, and
writes down what it was told. The real engine is a desktop application nobody
can install in a test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from halyard.agents.base import SessionRef
from halyard.agents.zcode import account, protocol
from halyard.agents.zcode import runner as delivery
from halyard.agents.zcode.protocol import Answer, Permission

SESSION = "sess_11111111-2222-3333-4444-555555555555"
WORKSPACE = {"workspacePath": "/tmp/somewhere"}

#: A stand-in engine. It answers what the real one answers, asks what the real
#: one asks, and keeps a record of both in `said.json`.
ENGINE = """
import json, os, sys

record = {"calls": [], "auth": None, "permissions": [], "headers": []}
where = os.environ["FAKE_RECORD"]
repeat = int(os.environ.get("FAKE_REPEAT", "1"))
after = os.environ.get("FAKE_AFTER", "completed")
# How big the session it hands back on resume is. The real one sends every
# message of it, on one line.
snapshot = int(os.environ.get("FAKE_SNAPSHOT", "0"))


def say(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


def keep():
    with open(where, "w") as file:
        json.dump(record, file)


say({"id": 1, "method": "session/requestRuntimePreferences", "params": {}})
asked = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method:
        record["calls"].append({"method": method, "params": message.get("params")})
        keep()
        if method == "session/list":
            say({"id": message["id"], "result": {"sessions": [
                {"sessionId": %r, "workspace": %r, "title": "a seat"}]}})
        elif method == "session/resume":
            say({"id": message["id"], "result": {"messages": "m" * snapshot}})
        elif method == "session/send":
            say({"id": message["id"], "result": {"accepted": True}})
            for _ in range(repeat):
                asked += 1
                say({"id": 100 + asked, "method": "interaction/requestPermission", "params": {
                    "sessionId": %r, "toolName": "Write", "riskLevel": "medium",
                    "requestId": "perm_one", "toolCallId": "call_one",
                    "input": {"file_path": "/tmp/somewhere/notes.md"}}})
        else:
            say({"id": message["id"], "result": {}})
        continue
    result = message.get("result") or {}
    if "headersApplied" in result:
        record["headers"].append(result)
        if "requestAuth" in result:
            record["auth"] = result
        keep()
        if os.environ.get("FAKE_CAPTCHA") and len(record["headers"]) == 1:
            # What the engine does when the provider demands a captcha: it asks
            # the same question again, for a token a solved one produces.
            say({"id": 3, "method": "interaction/requestProviderRuntimeHeaders",
                 "params": {"reason": "captcha-retry", "providerId": "account:plan"}})
        elif os.environ.get("FAKE_CAPTCHA"):
            say({"method": "session/update", "params": {"type": "turn.failed",
                 "payload": {"turnPhase": "execution", "error": {
                     "type": "ProviderError", "code": "3007",
                     "message": "Captcha verification request timed out"}}}})
        continue
    if "decision" in result:
        record["permissions"].append(result)
        keep()
        if len(record["permissions"]) >= repeat:
            if after == "failed":
                say({"method": "session/update", "params": {"type": "turn.failed",
                     "payload": {"turnPhase": "model_creation", "error": {
                         "type": "ProviderError", "message": "the model refused",
                         "code": "3103"}}}})
            else:
                say({"method": "session/update", "params": {"type": "turn.completed",
                     "payload": {"response": "DONE"}}})
        continue
    if result:
        # The answer to requestRuntimePreferences, which arrives before
        # anything else: now ask for the credential, as the engine does.
        say({"id": 2, "method": "interaction/requestProviderRuntimeHeaders", "params": {}})
"""


@pytest.fixture
def zcode(tmp_path: Path, monkeypatch):
    """A stand-in application, and a runner pointed at it."""
    app = tmp_path / "ZCode.app"
    engine = app / "Contents/Resources/glm/zcode.cjs"
    engine.parent.mkdir(parents=True)
    engine.write_text(ENGINE % (SESSION, WORKSPACE, SESSION))
    executable = app / "Contents/MacOS/ZCode"
    executable.parent.mkdir(parents=True)
    # The runner runs the executable with the engine as its first argument,
    # the way the application's own bundle does.
    executable.write_text('#!/bin/sh\nexec python3 "$1"\n')
    executable.chmod(0o755)

    record = tmp_path / "said.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    # The wait for models to materialise is the real engine's, not this one's.
    monkeypatch.setattr(delivery, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(delivery.trust, "app", lambda: app)
    monkeypatch.setattr(
        delivery.sessions,
        "find_session",
        lambda name, home=None: SessionRef(session_id=SESSION, name="a seat", cwd=str(tmp_path)),
    )
    monkeypatch.setattr(
        delivery.account,
        "read",
        lambda current, home=None: account.Account(
            catalog=tmp_path / "zcode-builtin.json",
            revision="28",
            providers={"account:plan": ["GLM-5.3-Flash"]},
            current=current,
        ),
    )
    return app, record


async def until(done, seconds: float = 5.0):
    """Wait for something the stand-in engine writes down."""
    for _ in range(int(seconds / 0.05)):
        if done():
            return True
        await asyncio.sleep(0.05)
    return done()


def said(record: Path) -> dict:
    try:
        return json.loads(record.read_text())
    except (OSError, ValueError):
        return {"calls": [], "auth": None, "permissions": [], "headers": []}


def a_runner(asking, **more) -> delivery.ZCodeRunner:
    return delivery.ZCodeRunner(
        token="a-plan-key",
        model="account:plan/GLM-5.3-Flash",
        asking=asking,
        **more,
    )


async def allowing(request: Permission) -> Answer:
    return Answer(decision="allow")


async def test_the_session_is_opened_told_to_ask_and_sent_to(zcode) -> None:
    """The order the engine was measured wanting: the account, the session, the
    mode — set after opening, or nothing is enforced — and then the message."""
    _, record = zcode
    sent = await a_runner(allowing).send(SESSION, "look at this")

    assert sent is True
    await until(lambda: said(record)["permissions"])
    methods = [call["method"] for call in said(record)["calls"]]
    assert methods[: methods.index("session/send") + 1] == [
        "provider/updateAccountConfig",
        "session/list",
        "session/resume",
        "session/setMode",
        "session/subscribe",
        "session/send",
    ]
    mode = next(c for c in said(record)["calls"] if c["method"] == "session/setMode")
    assert mode["params"] == {"sessionId": SESSION, "mode": "build"}
    resumed = next(c for c in said(record)["calls"] if c["method"] == "session/resume")
    assert resumed["params"]["workspace"] == WORKSPACE


async def test_the_credential_is_the_token_and_the_turn_pays_with_it(zcode) -> None:
    _, record = zcode

    await a_runner(allowing).send(SESSION, "look at this")

    assert await until(lambda: said(record)["auth"])
    assert said(record)["auth"] == {"headersApplied": True, "requestAuth": {"apiKey": "a-plan-key"}}


async def test_every_side_effect_is_asked_about_and_the_answer_goes_back(zcode) -> None:
    _, record = zcode
    asked: list[Permission] = []

    async def asking(request: Permission) -> Answer:
        asked.append(request)
        return Answer(decision="deny", reason="not this one")

    await a_runner(asking).send(SESSION, "write something")

    assert await until(lambda: said(record)["permissions"])
    assert said(record)["permissions"] == [{"decision": "deny", "reason": "not this one"}]
    [request] = asked
    assert (request.tool, request.risk, request.session_id) == ("Write", "medium", SESSION)
    assert request.input["file_path"] == "/tmp/somewhere/notes.md"
    assert request.about["session_name"] == "a seat"


async def test_the_same_question_asked_again_is_one_card(zcode) -> None:
    """The engine repeats an unanswered question every few seconds. That is the
    same question, and somebody's phone should say so once."""
    _, record = zcode
    asked = []

    async def slowly(request: Permission) -> Answer:
        asked.append(request)
        await asyncio.sleep(0.3)
        return Answer(decision="allow")

    await a_runner(slowly).send(SESSION, "write something")

    assert await until(lambda: said(record)["permissions"])
    assert len(asked) == 1, "one card for one requestId"


async def test_a_seat_is_not_sent_to_while_it_is_still_answering(zcode) -> None:
    _, _ = zcode
    one = a_runner(allowing)

    assert await one.send(SESSION, "first") is True
    assert one.busy(SESSION) is True
    assert await one.send(SESSION, "second") is False


async def test_a_turn_that_fails_is_told_to_whoever_asked(zcode, monkeypatch) -> None:
    """Delivery succeeded; the turn did not. Whoever sent it hears so."""
    monkeypatch.setenv("FAKE_AFTER", "failed")
    failures: list[str] = []

    async def note(why: str) -> None:
        failures.append(why)

    await a_runner(allowing).send(SESSION, "write something", when_done=note)

    assert await until(lambda: failures, seconds=8.0)
    # The engine's own words, where it keeps them: inside `error`, with the
    # phase it died in. "it failed" is not something anybody can act on.
    assert failures == ["the model refused (3103) in model_creation"]


async def test_a_captcha_is_refused_and_named_rather_than_waited_on(zcode, monkeypatch) -> None:
    """The provider demands a captcha by asking the host for the token a solved
    one produces. Only ZCode's own window can get that, so Halyard says so and
    the turn ends — rather than sending the same key again and leaving the seat
    on a turn that has already stopped meaning anything."""
    monkeypatch.setenv("FAKE_CAPTCHA", "1")
    _, record = zcode
    failures: list[str] = []

    async def note(why: str) -> None:
        failures.append(why)

    assert await a_runner(allowing).send(SESSION, "look at this", when_done=note) is True

    assert await until(lambda: failures, seconds=8.0)
    assert await until(lambda: len(said(record)["headers"]) == 2, seconds=8.0)
    assert said(record)["headers"][-1] == {
        "headersApplied": False,
        "errorMessage": "Halyard cannot answer a captcha; it has no window",
    }
    assert "captcha" in failures[0] and "ZCode's own window" in failures[0]


def test_a_failure_shape_nobody_knows_is_repeated_rather_than_swallowed() -> None:
    assert "3101" in protocol._why({"error": {"unexpected": "3101"}})
    assert protocol._why({}) == "it failed"


async def test_nothing_is_sent_without_a_token_or_a_model(zcode, caplog) -> None:
    _, record = zcode

    assert await delivery.ZCodeRunner(model="account:plan/GLM").send(SESSION, "hello") is False
    assert await delivery.ZCodeRunner(token="a-key").send(SESSION, "hello") is False
    assert said(record)["calls"] == []
    assert "ZCODE_TOKEN" in caplog.text and "ZCODE_MODEL" in caplog.text


async def test_a_runner_with_no_gate_refuses_every_tool(zcode) -> None:
    """Not a state anybody should reach, and it fails closed if they do."""
    _, record = zcode

    await delivery.ZCodeRunner(token="a-key", model="account:plan/GLM").send(SESSION, "write")

    assert await until(lambda: said(record)["permissions"])
    assert said(record)["permissions"][0]["decision"] == "deny"


async def test_a_session_bigger_than_a_readers_usual_line_still_arrives(zcode, monkeypatch) -> None:
    """`session/resume` answers with the whole session on one line, and a seat
    that has been working for a fortnight is megabytes of it. A reader that
    stops at the usual 64 KiB hears nothing the engine says afterwards — which
    is what the first live delivery was, and it looked like silence."""
    monkeypatch.setenv("FAKE_SNAPSHOT", str(256 * 1024))
    _, record = zcode

    assert await a_runner(allowing).send(SESSION, "look at this") is True
    assert [call["method"] for call in said(record)["calls"]][-1] == "session/send"


async def test_an_engine_that_will_not_run_is_said_so_rather_than_waited_out(zcode, caplog) -> None:
    """A machine where the engine dies at once must not hold the seat for half
    a minute per call and then report a timeout, which reads like a slow engine
    rather than a missing one."""
    app, _ = zcode
    (app / "Contents/Resources/glm/zcode.cjs").write_text("raise SystemExit(1)\n")

    began = asyncio.get_running_loop().time()
    sent = await a_runner(allowing).send(SESSION, "look at this")
    took = asyncio.get_running_loop().time() - began

    assert sent is False
    assert took < protocol.LISTENING_SECONDS
    assert "stopped" in caplog.text


# --- the account the engine is shown, and the gate it is answered from ---------


CATALOG = {
    "revision": "28",
    "config": {
        "providerConfigRules": {
            "providerRules": [
                {"providerId": "account:plan", "config": {"builtinModelIds": ["GLM-5.3-Flash"]}},
                {"providerId": "account:other", "config": {}},
            ]
        }
    },
}


def test_the_snapshot_marks_one_provider_current_and_hashes_the_catalogs_path(tmp_path) -> None:
    """The hash is of the path, not the file — measured, and a wrong one is
    accepted and then materialises no models at all."""
    import hashlib

    home = tmp_path / "home"
    catalog = home / account.RUNTIME / "3.12.3" / account.CATALOG
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(CATALOG))

    read = account.read("account:plan", home=home)

    assert read is not None
    snapshot = read.snapshot(now=1_700_000_000)
    digest = hashlib.sha256(str(catalog.resolve()).encode("utf-8")).hexdigest()
    assert snapshot["basedOnZCodeBuiltinRevision"] == f"zcode-builtin:28:{digest}"
    assert snapshot["states"]["account:plan"]["current"] is True
    assert snapshot["states"]["account:other"]["current"] is False
    assert snapshot["providers"]["account:plan"]["access"] == {
        "type": "zhipu-account",
        "entitled": True,
    }


def test_no_catalog_is_no_account(tmp_path) -> None:
    assert account.read("account:plan", home=tmp_path) is None


class Answering:
    """A control plane that answers one way."""

    def __init__(self, said: dict | Exception) -> None:
        self.said = said
        self.asked: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, json: dict):
        self.asked.append(json)
        if isinstance(self.said, Exception):
            raise self.said
        return Said(self.said)


class Said:
    def __init__(self, body: dict) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


def asking_about() -> Permission:
    return Permission(
        tool="Bash",
        input={"command": "rm -rf build"},
        risk="high",
        request_id="perm_one",
        tool_call_id="call_one",
        session_id=SESSION,
        about={"cwd": "/code/alpha-engine", "session_name": "zdrv"},
    )


async def test_the_gate_is_asked_the_way_every_bridge_asks_it(monkeypatch) -> None:
    from halyard.agents.zcode import gate

    control = Answering({"decision": "allow", "reason": "approved by tg:1"})
    monkeypatch.setattr(gate.httpx, "AsyncClient", control)

    answer = await gate.through("http://127.0.0.1:8787", timeout=5)(asking_about())

    assert answer.decision == "allow"
    [asked] = control.asked
    assert asked["session_id"] == SESSION
    assert (asked["tool"], asked["command"]) == ("Bash", "rm -rf build")
    assert (asked["agent_id"], asked["declared_risk"]) == ("zcode", "high")
    assert asked["session_name"] == "zdrv"


async def test_a_paused_gate_refuses_rather_than_leaving_the_turn_hanging(monkeypatch) -> None:
    from halyard.agents.zcode import gate

    monkeypatch.setattr(gate.httpx, "AsyncClient", Answering({"decision": "defer", "reason": ""}))

    answer = await gate.through("http://127.0.0.1:8787", timeout=5)(asking_about())

    assert answer.decision == "deny"
    assert "paused" in answer.reason


async def test_a_control_plane_that_cannot_be_reached_refuses(monkeypatch) -> None:
    import httpx

    from halyard.agents.zcode import gate

    monkeypatch.setattr(gate.httpx, "AsyncClient", Answering(httpx.ConnectError("no")))

    answer = await gate.through("http://127.0.0.1:8787", timeout=5)(asking_about())

    assert answer.decision == "deny"
