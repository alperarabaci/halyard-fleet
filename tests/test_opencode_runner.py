"""Tests for putting a message into a running opencode session.

The other three runners start a process. This one writes into one that is
already running and holding the conversation somebody is working in, so what
matters here is the shape of what it sends and that it never raises — the
caller is a poll loop that has to stay alive to read the next message.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

from halyard.agents.base import AgentRunner
from halyard.agents.opencode.runner import OpencodeRunner


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, dict]]:
    """Every POST this runner would have made."""
    posted: list[tuple[str, dict]] = []

    def record(where: str, body: dict) -> bool:
        posted.append((where, body))
        return True

    monkeypatch.setattr(OpencodeRunner, "_post", staticmethod(record))
    return posted


def test_it_is_the_shape_the_channel_expects() -> None:
    """The protocol is what makes a runtime addable without editing the
    channel, so a runner that only nearly matches it is worth catching here."""
    assert isinstance(OpencodeRunner(), AgentRunner)


async def test_a_message_lands_in_the_session_it_was_addressed_to(sent) -> None:
    await OpencodeRunner().send("ses_1", "carry on")

    where, body = sent[0]
    assert "/session/ses_1/prompt_async" in where
    assert body["parts"] == [{"type": "text", "text": "carry on"}]


async def test_no_model_is_sent_when_none_was_chosen(sent) -> None:
    """Somebody who has not chosen from the phone has chosen at the desk, and
    overriding that silently is the opposite of what they did."""
    await OpencodeRunner().send("ses_1", "carry on")

    assert "model" not in sent[0][1]


async def test_a_chosen_model_is_split_the_way_opencode_names_them(sent) -> None:
    """`provider/model`, from one end: a model id may carry slashes of its own."""
    runner = OpencodeRunner()
    runner.set_model("ses_1", "zai-coding-plan/glm-5.3-flash")

    await runner.send("ses_1", "carry on")

    assert sent[0][1]["model"] == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.3-flash",
    }


async def test_giving_the_model_back_stops_sending_one(sent) -> None:
    runner = OpencodeRunner()
    runner.set_model("ses_1", "deepseek/deepseek-v4-pro")
    runner.set_model("ses_1", None)

    await runner.send("ses_1", "carry on")

    assert "model" not in sent[0][1]


async def test_the_model_is_remembered_per_session(sent) -> None:
    runner = OpencodeRunner()
    runner.set_model("ses_1", "deepseek/deepseek-v4-pro")

    await runner.send("ses_2", "carry on")

    assert "model" not in sent[0][1]
    assert runner.preferences("ses_1") == ("deepseek/deepseek-v4-pro", None)


async def test_the_project_is_named_so_the_right_one_is_written_to(sent) -> None:
    await OpencodeRunner().send("ses_1", "carry on", cwd="/a/project")

    assert "directory=%2Fa%2Fproject" in sent[0][0]


async def test_nothing_is_sent_for_an_empty_message(sent) -> None:
    runner = OpencodeRunner()

    assert await runner.send("ses_1", "   ") is False
    assert await runner.send("", "carry on") is False
    assert sent == []


# --- it must not raise --------------------------------------------------------


async def test_a_refusal_is_reported_rather_than_raised(monkeypatch) -> None:
    """opencode saying no — a model it does not have, a session deleted — is
    news for the person waiting, not an exception into the poll loop."""

    import io

    def refuse(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1/x", 400, "Bad Request", {}, io.BytesIO(b'{"error":"no such model"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", refuse)

    assert await OpencodeRunner().send("ses_1", "carry on") is False


async def test_an_opencode_that_is_not_running_is_reported_rather_than_raised(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(URLError("refused"))
    )

    assert await OpencodeRunner().send("ses_1", "carry on") is False


# --- what can be chosen -------------------------------------------------------


def test_the_models_offered_are_the_ones_configured(monkeypatch) -> None:
    from halyard.core.config_file import RuntimeSettings

    monkeypatch.setattr(
        "halyard.core.config_file.runtime_settings",
        lambda *a, **k: {
            "opencode": RuntimeSettings(name="opencode", models=("a-model", "b-model"))
        },
    )

    offered = OpencodeRunner().options()

    assert offered["model"] == (("a-model", "b-model"), False), "a hint, not a gate"


def test_nothing_is_offered_when_nothing_is_configured(monkeypatch) -> None:
    """Better than a stale list: opencode has thirty-three providers and the
    one worth offering is the one somebody wrote down."""
    monkeypatch.setattr("halyard.core.config_file.runtime_settings", lambda *a, **k: {})

    assert OpencodeRunner().options() == {}


def test_effort_is_accepted_and_has_nowhere_to_go() -> None:
    """A Claude Code and Codex idea. opencode's message carries a model and
    nothing about how hard it thinks, so claiming to set one would be a setting
    dropped in transit."""
    runner = OpencodeRunner()

    runner.set_effort("ses_1", "high")

    assert runner.preferences("ses_1") == (None, None)


def test_json_is_what_goes_on_the_wire(sent) -> None:
    """The body is built as a dict and has to survive being one."""
    import asyncio

    asyncio.run(OpencodeRunner().send("ses_1", "carry on"))

    assert json.dumps(sent[0][1])


def test_effort_is_refused_by_the_channel_rather_than_pretended_here() -> None:
    """opencode does have this axis — it calls it a variant, and lowering
    reasoning is done that way. It is a choice the interface keeps: measured in
    1.18.29, `command.model.variant.cycle` is a keybinding and the message body
    has no field for it.

    So the runner offers no effort, which is what makes the channel refuse
    `/effort` instead of confirming a setting that went nowhere.
    """
    assert "effort" not in OpencodeRunner().options()


async def test_it_does_not_wait_for_the_turn_to_finish(sent) -> None:
    """`/session/{id}/message` runs the turn and answers when it is done.
    Measured: a message from a phone reached the session, opencode started
    working, and thirty seconds later this reported "that did not reach" while
    the reply was appearing on the screen. The reply comes back through the
    plugin like every other one; nothing here is waiting for it.
    """
    await OpencodeRunner().send("ses_1", "carry on")

    where, _ = sent[0]
    assert where.endswith("/prompt_async") or "/prompt_async?" in where
    assert "/message" not in where


# --- a turn of Halyard's own ----------------------------------------------------


class Server:
    """The opencode API as `ask` meets it: every call remembered, each answered
    as a real server answered it when this was measured."""

    def __init__(self, *, says: str = "1790280300", hangs: bool = False) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.says = says
        self.hangs = hangs

    def __call__(self, method: str, where: str, body: dict | None, timeout: float):
        self.calls.append((method, where, body))
        path = where.split("?")[0].removeprefix("http://127.0.0.1:4096")
        if method == "POST" and path == "/session":
            return {"id": "ses_own1"}
        if method == "POST" and path.endswith("/message"):
            if self.hangs:
                import time

                time.sleep(1)
            return {
                "info": {"role": "assistant", "tokens": {"input": 96, "output": 8}},
                "parts": [{"type": "step-start"}, {"type": "text", "text": self.says}],
            }
        if method == "GET" and path.endswith("/message"):
            return [
                {"info": {"role": "user"}},
                {
                    "info": {
                        "role": "assistant",
                        "providerID": "zai-coding-plan",
                        "modelID": "glm-5.3",
                        "cost": 0,
                        "tokens": {
                            "input": 52,
                            "output": 13,
                            "reasoning": 20,
                            "cache": {"read": 34496, "write": 0},
                        },
                    }
                },
                {
                    "info": {
                        "role": "assistant",
                        "providerID": "zai-coding-plan",
                        "modelID": "glm-5.3",
                        "cost": 0,
                        "tokens": {
                            "input": 96,
                            "output": 8,
                            "reasoning": 0,
                            "cache": {"read": 34496, "write": 0},
                        },
                    }
                },
            ]
        return {}


@pytest.fixture
def server(monkeypatch) -> Server:
    answering = Server()
    monkeypatch.setattr(OpencodeRunner, "_call", staticmethod(answering))
    monkeypatch.setattr("halyard.agents.opencode._port", lambda: 4096)
    return answering


async def test_a_turn_of_its_own_runs_in_a_session_of_its_own_and_leaves_none(
    server: Server, tmp_path
) -> None:
    """Opened, asked, ended and deleted — in the opencode already running, in
    the project's directory, with the model and its variant on the message."""
    said = await OpencodeRunner().ask(
        "run date +%s",
        model="zai-coding-plan/glm-5.3",
        effort="high",
        cwd=tmp_path,
        edits=False,
        purpose="inspect proof · repeat",
    )

    assert said == "1790280300"
    steps = [(method, where.split("?")[0].rsplit("/", 1)[-1]) for method, where, _ in server.calls]
    assert steps == [
        ("POST", "session"),
        ("POST", "message"),
        ("GET", "message"),
        ("POST", "abort"),
        ("DELETE", "ses_own1"),
    ]
    assert all("directory=" in where for _, where, _ in server.calls)
    _, _, opened = server.calls[0]
    assert opened["title"] == "halyard: inspect proof · repeat"
    _, _, message = server.calls[1]
    assert message["model"] == {"providerID": "zai-coding-plan", "modelID": "glm-5.3"}
    assert message["variant"] == "high"
    assert message["parts"] == [{"type": "text", "text": "run date +%s"}]


async def test_a_turn_that_edits_nothing_cannot_edit_but_still_asks(server: Server) -> None:
    """Denied on top of the project's rules, which stay: measured, a session's
    rules come last, so `bash: ask` still sends a command to Halyard."""
    await OpencodeRunner().ask("look", edits=False)

    _, _, opened = server.calls[0]
    assert {"permission": "edit", "pattern": "*", "action": "deny"} in opened["permission"]
    assert {"permission": "webfetch", "pattern": "*", "action": "deny"} in opened["permission"]
    assert not any(rule["permission"] == "bash" for rule in opened["permission"])


async def test_whoever_started_it_hears_its_session_before_the_message_goes(
    server: Server,
) -> None:
    """Its questions and its reply arrive under opencode's id, not the caller's."""
    heard: list[tuple[str, int]] = []

    await OpencodeRunner().ask(
        "look",
        session_id="the-callers-id",
        started=lambda ident: heard.append((ident, len(server.calls))),
    )

    assert heard == [("ses_own1", 1)], "told after the session opened, before the message"


async def test_every_step_s_tokens_are_one_row_under_the_caller_s_id(
    server: Server, tmp_path
) -> None:
    """What joins the record the caller keeps; the model's reasoning counted
    with what it wrote."""
    import sqlite3

    database = tmp_path / "halyard.db"

    await OpencodeRunner(usage_path=database).ask(
        "look",
        session_id="the-callers-id",
        model="zai-coding-plan/glm-5.3",
        purpose="inspect proof · repeat",
        project="alpha-engine",
    )

    with sqlite3.connect(database) as db:
        [row] = db.execute(
            "SELECT runtime, session_id, model, purpose, project, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens FROM turn_usage"
        ).fetchall()
    assert row == (
        "opencode",
        "the-callers-id",
        "zai-coding-plan/glm-5.3",
        "inspect proof · repeat",
        "alpha-engine",
        148,
        41,
        0,
        68992,
    )


async def test_a_turn_stopped_part_way_is_ended_and_deleted(monkeypatch) -> None:
    """Somebody pressed Stop: the turn ends where it runs rather than carrying
    on for nobody, and its session goes."""
    import asyncio

    hanging = Server(hangs=True)
    monkeypatch.setattr(OpencodeRunner, "_call", staticmethod(hanging))
    monkeypatch.setattr("halyard.agents.opencode._port", lambda: 4096)

    turn = asyncio.ensure_future(OpencodeRunner().ask("look"))
    await asyncio.sleep(0.2)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    methods = [method for method, _, _ in hanging.calls]
    assert methods[-2:] == ["POST", "DELETE"]
    assert hanging.calls[-2][1].split("?")[0].endswith("/abort")


async def test_an_opencode_that_is_not_there_is_no_answer(monkeypatch) -> None:
    monkeypatch.setattr(OpencodeRunner, "_call", staticmethod(lambda *args: None))
    monkeypatch.setattr("halyard.agents.opencode._port", lambda: 4096)

    assert await OpencodeRunner().ask("look") is None
