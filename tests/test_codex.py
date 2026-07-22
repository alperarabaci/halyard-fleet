"""The Codex runtime adapter.

Everything asserted here comes from a measurement recorded in
`docs/codex-adapter-findings.md` or in the follow-up runs noted in the module
docstrings. Where Codex differs from Claude Code the difference is the point of
the test, because those are the places a shared protocol quietly gets one of
its runtimes wrong.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from halyard.agents.codex import CodexRunner, find_session, list_named_sessions


def codex_home(root: Path, *, sessions: list[dict], rollouts: dict[str, dict]) -> Path:
    """A fake ~/.codex with the two files this adapter reads."""
    (root / "sessions" / "2026" / "07" / "22").mkdir(parents=True)
    index = root / "session_index.jsonl"
    index.write_text("".join(json.dumps(entry) + "\n" for entry in sessions))
    for session_id, context in rollouts.items():
        day = root / "sessions" / "2026" / "07" / "22"
        path = day / f"rollout-2026-07-22T10-00-00-{session_id}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": context.get("cwd", "")}})
            + "\n"
            + json.dumps({"type": "turn_context", "payload": context})
            + "\n"
        )
    return root


# --- finding a session ------------------------------------------------------


def test_the_name_comes_from_the_index_and_the_rest_from_the_transcript(tmp_path: Path) -> None:
    """Codex splits what Claude Code keeps together.

    Nothing in a rollout carries the thread name, so a transcript alone cannot
    say what a session is called; and the index carries no directory, so the
    index alone cannot say where it runs. Both files or nothing.
    """
    root = codex_home(
        tmp_path,
        sessions=[{"id": "abc", "thread_name": "driver", "updated_at": "2026-07-22T10:00:00Z"}],
        rollouts={"abc": {"cwd": "/repo", "model": "gpt-5.6-sol", "effort": "high"}},
    )

    ref = find_session("driver", root=root)

    assert ref is not None
    assert (ref.session_id, ref.cwd) == ("abc", "/repo")
    assert (ref.model, ref.effort) == ("gpt-5.6-sol", "high")


def test_a_raw_session_id_also_resolves(tmp_path: Path) -> None:
    """`codex exec resume` takes either, so refusing an id here would be ours."""
    root = codex_home(
        tmp_path,
        sessions=[{"id": "abc", "thread_name": "driver"}],
        rollouts={"abc": {"cwd": "/repo"}},
    )

    assert find_session("abc", root=root) is not None


def test_a_renamed_session_resolves_to_its_latest_name(tmp_path: Path) -> None:
    """The index is append-only, so an old line still names an old name."""
    root = codex_home(
        tmp_path,
        sessions=[
            {"id": "abc", "thread_name": "old", "updated_at": "2026-07-22T09:00:00Z"},
            {"id": "abc", "thread_name": "new", "updated_at": "2026-07-22T11:00:00Z"},
        ],
        rollouts={"abc": {"cwd": "/repo"}},
    )

    assert find_session("new", root=root) is not None
    assert list_named_sessions(root=root)[0][0] == "new"


def test_the_newest_turn_wins_over_the_first(tmp_path: Path) -> None:
    """A long conversation's model is the one it is on now, not the one it opened with.

    Which is the whole point of showing it: in a navigator/driver pair the two
    are deliberately different, and the stale answer looks just as plausible.
    """
    root = tmp_path
    (root / "sessions" / "2026" / "07" / "22").mkdir(parents=True)
    (root / "session_index.jsonl").write_text(
        json.dumps({"id": "abc", "thread_name": "seat"}) + "\n"
    )
    (root / "sessions" / "2026" / "07" / "22" / "rollout-2026-07-22T10-00-00-abc.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/repo", "model": "gpt-5.2"}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"cwd": "/repo", "model": "gpt-5.6-sol"}})
        + "\n"
    )

    assert find_session("seat", root=root).model == "gpt-5.6-sol"


def test_an_unknown_name_is_none_rather_than_a_guess(tmp_path: Path) -> None:
    root = codex_home(tmp_path, sessions=[{"id": "abc", "thread_name": "driver"}], rollouts={})

    assert find_session("navigator", root=root) is None


# --- what can be chosen -----------------------------------------------------


def runner_with_catalog(**kwargs) -> CodexRunner:
    made = CodexRunner(**kwargs)
    made._binary = "/opt/homebrew/bin/codex"
    made._catalog = {
        "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "gpt-5.5": ("low", "medium", "high", "xhigh"),
    }
    return made


def test_effort_is_reported_for_the_model_the_session_is_on() -> None:
    """The reason `options()` grew a session argument.

    Measured from the CLI's own catalog: `ultra` exists on gpt-5.6-sol and not
    on gpt-5.5. One list for both would offer a level the model then refuses,
    in the one place somebody looks in order not to be refused.
    """
    runner = runner_with_catalog()
    runner.set_model("s1", "gpt-5.6-sol")
    runner.set_model("s2", "gpt-5.5")

    assert "ultra" in runner.options("s1")["effort"][0]
    assert "ultra" not in runner.options("s2")["effort"][0]


def test_models_are_offered_but_not_enforced() -> None:
    """Same rule as everywhere: the catalog is a hint, the CLI is the authority."""
    runner = runner_with_catalog()

    values, enforced = runner.options("s1")["model"]

    assert "gpt-5.6-sol" in values
    assert enforced is False


def test_effort_is_enforced() -> None:
    assert runner_with_catalog().options("s1")["effort"][1] is True


def test_an_unknown_model_falls_back_to_every_level_rather_than_none() -> None:
    """Refusing everything would be worse than offering one the model rejects
    with a message of its own."""
    runner = runner_with_catalog()
    runner.set_model("s1", "gpt-6-unreleased")

    assert set(runner.options("s1")["effort"][0]) >= {"low", "ultra"}


# --- sending ----------------------------------------------------------------


class FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def spying(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_exec(*arguments, **kwargs):
        calls.append({"argv": list(arguments), "cwd": kwargs.get("cwd")})
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_a_message_resumes_the_session_by_id(monkeypatch) -> None:
    calls = spying(monkeypatch)

    await runner_with_catalog().send("abc", "carry on", cwd="/repo")

    argv = calls[0]["argv"]
    assert argv[1:3] == ["exec", "resume"]
    assert argv[-2:] == ["abc", "carry on"]


async def test_effort_travels_as_a_config_override(monkeypatch) -> None:
    """There is no --effort flag. It is a TOML config value, quotes included."""
    calls = spying(monkeypatch)
    runner = runner_with_catalog()
    runner.set_effort("abc", "xhigh")

    await runner.send("abc", "carry on", cwd="/repo")

    argv = calls[0]["argv"]
    assert "-c" in argv
    assert 'model_reasoning_effort="xhigh"' in argv


async def test_the_session_runs_in_its_own_directory(monkeypatch, tmp_path: Path) -> None:
    """A gate question, not a tidiness one.

    Resume finds a session from anywhere, so nothing fails if the directory is
    wrong. But Codex resolves project hooks from the CLI process's working
    directory, so a session resumed from elsewhere runs under a different
    project's gate — or under none — and looks completely normal doing it.
    """
    calls = spying(monkeypatch)
    root = codex_home(
        tmp_path,
        sessions=[{"id": "abc", "thread_name": "driver"}],
        rollouts={"abc": {"cwd": "/the/session/repo"}},
    )
    monkeypatch.setattr(
        "halyard.agents.codex.runner.find_session",
        lambda name: find_session(name, root=root),
    )

    await runner_with_catalog().send("abc", "carry on")

    assert calls[0]["cwd"] == "/the/session/repo"


async def test_a_session_with_no_recorded_directory_is_refused(monkeypatch) -> None:
    """Better to deliver nothing than to deliver it past the wrong gate."""
    calls = spying(monkeypatch)
    monkeypatch.setattr("halyard.agents.codex.runner.find_session", lambda name: None)

    assert await runner_with_catalog().send("abc", "carry on") is False
    assert calls == []


async def test_two_messages_to_one_session_are_serialised(monkeypatch) -> None:
    """One measured pair of overlapping resumes survived. One trial is not a
    guarantee, the equivalent on Claude Code forks silently, and the cost of
    being wrong in this direction is a queue rather than a lost turn."""
    started = asyncio.Event()
    release = asyncio.Event()
    inside = 0
    most_at_once = 0

    async def fake_exec(*arguments, **kwargs):
        nonlocal inside, most_at_once
        inside += 1
        most_at_once = max(most_at_once, inside)
        started.set()
        await release.wait()
        inside -= 1
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = runner_with_catalog()

    first = asyncio.create_task(runner.send("abc", "one", cwd="/repo"))
    await started.wait()
    second = asyncio.create_task(runner.send("abc", "two", cwd="/repo"))
    # Give the second every chance to slip in beside the first.
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert most_at_once == 1
