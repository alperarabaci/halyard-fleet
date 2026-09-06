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
    assert "/session/ses_1/message" in where
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
