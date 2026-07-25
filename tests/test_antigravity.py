"""The Antigravity adapter.

Everything asserted here was measured against a live Antigravity, and the
measurements are recorded in `docs/antigravity-payload-notes.md`. Where this
runtime differs from the other two the difference is the point of the test —
those are the places a shared protocol quietly gets one of its runtimes wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from halyard.agents.antigravity import AntigravityRunner, find_session, list_named_sessions

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"


def antigravity_home(root: Path, conversations: dict[str, str | None]) -> Path:
    """A fake ~/.gemini/antigravity: annotations for names, brain for ids."""
    (root / "annotations").mkdir(parents=True)
    for conversation_id, title in conversations.items():
        (root / "brain" / conversation_id).mkdir(parents=True)
        line = f'title:"{title}" ' if title else ""
        (root / "annotations" / f"{conversation_id}.pbtxt").write_text(
            line + "last_user_view_time:{seconds:1784928949 nanos:811000000}"
        )
    return root


# --- finding a conversation --------------------------------------------------


def test_a_name_comes_from_the_annotation_file(tmp_path: Path) -> None:
    """Nothing else carries it.

    The transcript has `content`, `created_at`, `source`, `status`,
    `step_index`, `thinking`, `tool_calls` and `type` — no name, no working
    directory, no model. Claude Code has all three in its transcript.
    """
    root = antigravity_home(tmp_path, {"abc": "alpha-engine-driver"})

    found = find_session("alpha-engine-driver", root=root)

    assert found is not None
    assert found.session_id == "abc"


def test_a_conversation_nobody_renamed_has_no_name(tmp_path: Path) -> None:
    """Its annotation file exists and simply has no `title:` field."""
    root = antigravity_home(tmp_path, {"abc": None})

    assert find_session("anything", root=root) is None
    assert list_named_sessions(root=root) == []


def test_a_raw_conversation_id_resolves_too(tmp_path: Path) -> None:
    """Telling somebody to go and name a conversation before it can be
    addressed is not an answer when the id is in front of them."""
    root = antigravity_home(tmp_path, {"abc": None})

    assert find_session("abc", root=root) is not None


def test_a_name_is_matched_however_it_is_typed(tmp_path: Path) -> None:
    root = antigravity_home(tmp_path, {"abc": "Alpha-Engine-Driver"})

    assert find_session("  alpha-engine-driver ", root=root) is not None


def test_the_directory_is_left_unset(tmp_path: Path) -> None:
    """It is knowable, but only from the running application.

    A listing should not fail because an app is closed, so `cwd` stays empty
    here and the runner asks when it needs it. The gate does not need it at
    all: `workspacePaths` arrives with every hook call.
    """
    root = antigravity_home(tmp_path, {"abc": "seat"})

    assert find_session("seat", root=root).cwd is None


# --- what can be chosen ------------------------------------------------------


def test_three_model_tiers_and_no_effort() -> None:
    """Antigravity takes `--model=<flash_lite|flash|pro>` and has no effort
    setting. The empty set is reported rather than omitted, so `/options` says
    so instead of leaving somebody wondering why `/effort` does nothing."""
    options = AntigravityRunner().options()

    assert options["model"][0] == ("flash_lite", "flash", "pro")
    assert options["effort"][0] == ()


def test_setting_an_effort_is_accepted_and_ignored() -> None:
    """Raising would break a channel that offers one command per runtime."""
    runner = AntigravityRunner()

    runner.set_effort("abc", "high")

    assert runner.preferences("abc")[1] is None


# --- delivery ----------------------------------------------------------------


class FakeProcess:
    returncode = 0

    def __init__(self, stdout: bytes = b"{}") -> None:
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def spying(monkeypatch, stdout: bytes = b"{}") -> list[dict]:
    calls: list[dict] = []

    async def fake_exec(*arguments, **kwargs):
        calls.append({"argv": list(arguments), "env": kwargs.get("env") or {}})
        return FakeProcess(stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def runner_with_endpoint(monkeypatch, endpoints=(("127.0.0.1:60762", "tok"),)):
    made = AntigravityRunner()
    made._binary = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
    monkeypatch.setattr(
        "halyard.agents.antigravity.runner.language_server_endpoints", lambda: list(endpoints)
    )
    return made


async def test_what_goes_out_is_a_wake_and_not_the_message(monkeypatch) -> None:
    """`agentapi send-message` can only file a SYSTEM_MESSAGE.

    Measured: `--title`, its one flag, leaves `sender=system` untouched, so
    anything sent this way arrives under a "Message from System" header with
    Antigravity's own sentence saying the user did not send it. The person's
    words go by `injectSteps` instead; this call only starts a turn.
    """
    calls = spying(monkeypatch)

    assert await runner_with_endpoint(monkeypatch).send("abc", "carry on") is True

    argv = calls[0]["argv"]
    assert argv[1:3] == ["agentapi", "send-message"]
    assert argv[-2] == "abc"
    assert "carry on" not in argv[-1], "the message must not go out as a system message"


async def test_the_address_and_token_are_passed_as_environment(monkeypatch) -> None:
    """Neither is published to a file; both come off the running process."""
    calls = spying(monkeypatch)

    await runner_with_endpoint(monkeypatch).send("abc", "carry on")

    assert calls[0]["env"]["ANTIGRAVITY_LS_ADDRESS"] == "127.0.0.1:60762"
    assert calls[0]["env"]["ANTIGRAVITY_CSRF_TOKEN"] == "tok"


async def test_nothing_is_sent_when_the_application_is_not_running(monkeypatch) -> None:
    """Unlike the other two runtimes there is no CLI to fall back on, so this
    is a knowable failure rather than a surprise."""
    calls = spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch, endpoints=())

    assert await runner.send("abc", "carry on") is False
    assert calls == []


async def test_an_error_inside_a_zero_exit_is_still_a_failure(monkeypatch) -> None:
    """agentapi answers with an error in the body often enough that the exit
    code alone is not the answer."""
    spying(monkeypatch, stdout=b'{"error": "rpc error: Unauthenticated"}')

    assert await runner_with_endpoint(monkeypatch).send("abc", "carry on") is False


async def test_each_candidate_port_is_tried(monkeypatch) -> None:
    """It listens on several and only one speaks gRPC. Which one is not
    predictable, so they are tried rather than guessed."""
    attempts: list[str] = []

    async def fake_exec(*arguments, **kwargs):
        address = (kwargs.get("env") or {})["ANTIGRAVITY_LS_ADDRESS"]
        attempts.append(address)
        return FakeProcess(b"{}" if address == "127.0.0.1:60762" else b'{"error": "preface"}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = runner_with_endpoint(
        monkeypatch, endpoints=(("127.0.0.1:60761", "t"), ("127.0.0.1:60762", "t"))
    )

    assert await runner.send("abc", "carry on") is True
    assert attempts == ["127.0.0.1:60761", "127.0.0.1:60762"]
    # And the one that worked is tried first next time.
    assert runner._endpoint == ("127.0.0.1:60762", "t")


# --- the two stores ----------------------------------------------------------


def two_stores(monkeypatch, tmp_path: Path, app: str, cli: str) -> None:
    """A machine with both Antigravities on it, each owning one conversation.

    They share nothing — not a directory, not a database, not a listing. This
    is why a conversation started in `agy` never appears in the application.
    """
    antigravity_home(tmp_path / "app", {app: "in-the-app"})
    antigravity_home(tmp_path / "cli", {cli: "in-the-cli"})
    monkeypatch.setattr("halyard.agents.antigravity.sessions.APP_HOME", tmp_path / "app")
    monkeypatch.setattr("halyard.agents.antigravity.sessions.CLI_HOME", tmp_path / "cli")


async def test_a_conversation_the_cli_owns_is_refused(monkeypatch, tmp_path: Path) -> None:
    """Refused rather than attempted, because the attempt would look like it worked.

    `agy --help` calls `--conversation <id>` "Resume a previous conversation by
    ID". Measured three times, it is not: it starts a *new* conversation seeded
    with a summary of the one named. The database is the proof — the referenced
    conversation kept its 13 steps and never saw the text, the new one recorded
    4, and `parent_references` was empty in both.

    So sending would exit 0 while the text was answered in a conversation
    nobody is watching, and the seat somebody is watching would sit there
    looking idle. A visible failure is the better of the two.
    """
    two_stores(monkeypatch, tmp_path, app="app-conversation", cli="cli-conversation")
    calls = spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch)

    assert await runner.send("cli-conversation", "carry on") is False
    assert calls == [], "nothing may be run for a conversation that cannot be resumed"


async def test_a_conversation_the_application_owns_is_still_delivered(
    monkeypatch, tmp_path: Path
) -> None:
    """The refusal above is about one store, not about Antigravity.

    Asserted beside it because a guard that turns out to reject everything
    passes its own test perfectly well.
    """
    two_stores(monkeypatch, tmp_path, app="app-conversation", cli="cli-conversation")
    calls = spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch)

    assert await runner.send("app-conversation", "carry on") is True
    assert calls[0]["argv"][1:3] == ["agentapi", "send-message"]


# --- the bridge --------------------------------------------------------------


@pytest.fixture
def bridge_module():
    sys.path.insert(0, str(BRIDGE))
    try:
        import hook_bridge

        yield hook_bridge
    finally:
        sys.path.remove(str(BRIDGE))


ANTIGRAVITY_PAYLOAD = {
    "toolCall": {"name": "run_command", "args": {"CommandLine": "npm test", "Cwd": "/w/p"}},
    "stepIdx": 19,
    "conversationId": "abc",
    "workspacePaths": ["/w/p"],
    "transcriptPath": (
        "/Users/me/.gemini/antigravity/brain/abc/.system_generated/logs/transcript.jsonl"
    ),
}


def test_the_bridge_reads_antigravitys_own_field_names(bridge_module) -> None:
    """`toolCall.args.CommandLine`, not `tool_input.command`. A third spelling
    of the same idea, and the matcher has been wrong once already for this."""
    body = bridge_module.build_body(ANTIGRAVITY_PAYLOAD)

    assert body["agent_id"] == "antigravity"
    assert body["tool"] == "run_command"
    assert body["command"] == "npm test"
    assert body["cwd"] == "/w/p"


def test_the_workspace_arrives_with_the_call(bridge_module) -> None:
    """Which matters here more than elsewhere: nothing in an Antigravity
    transcript records where the work is happening."""
    payload = {**ANTIGRAVITY_PAYLOAD, "toolCall": {"name": "run_command", "args": {}}}

    assert bridge_module.build_body(payload)["cwd"] == "/w/p"


@pytest.mark.parametrize(
    ("runtime", "expected_key", "forbidden_key"),
    [
        ("antigravity", "decision", "hookSpecificOutput"),
        ("claude-code", "hookSpecificOutput", "decision"),
        ("codex", "hookSpecificOutput", "decision"),
    ],
)
def test_each_runtime_gets_its_own_dialect_and_only_its_own(
    bridge_module, runtime: str, expected_key: str, forbidden_key: str
) -> None:
    """The measured safety property, and the reason the shapes are not merged.

    Against a live Antigravity, with a hook proven by a witness file to have
    fired, on a command nobody had approved before:

        hookSpecificOutput alone     the command ran
        both keys in one object      the command ran
        flat `decision` alone        blocked, with no prompt at all

    The extra key is not ignored — it stops the answer being understood, and an
    answer that is not understood is an approval.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        bridge_module.emit("PreToolUse", "deny", "because", runtime)

    answer = json.loads(buffer.getvalue())
    assert expected_key in answer
    assert forbidden_key not in answer


# --- who a message looks like it came from ------------------------------------


async def test_the_message_waits_to_be_collected(monkeypatch) -> None:
    """It is held for the `PreInvocation` hook, which can inject a user turn."""
    spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch)

    await runner.send("abc", "uyudun mu?")

    assert runner.take_pending("abc") == ["uyudun mu?"]


async def test_a_collected_message_is_not_handed_over_twice(monkeypatch) -> None:
    """`PreInvocation` fires before *every* model call, so a queue that was not
    emptied by the reader would put one sentence into every step of the turn."""
    spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch)
    await runner.send("abc", "uyudun mu?")

    runner.take_pending("abc")

    assert runner.take_pending("abc") == []


async def test_messages_are_collected_in_the_order_they_were_sent(monkeypatch) -> None:
    """Two sent while the agent was busy are one turn's worth of context, and
    read backwards they are a different conversation."""
    spying(monkeypatch)
    runner = runner_with_endpoint(monkeypatch)

    await runner.send("abc", "first")
    await runner.send("abc", "second")

    assert runner.take_pending("abc") == ["first", "second"]


async def test_a_message_that_could_not_wake_anything_is_dropped(monkeypatch) -> None:
    """Nothing woke, so nothing will come and collect it.

    Left queued, it would be injected into whatever turn happened next —
    possibly hours later, about something else entirely.
    """
    runner = runner_with_endpoint(monkeypatch, endpoints=())

    assert await runner.send("abc", "carry on") is False
    assert runner.take_pending("abc") == []


# --- an approval that actually approves --------------------------------------


def emitted(bridge_module, decision: str, runtime: str, grant: str | None = None) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        bridge_module.emit("PreToolUse", decision, "approved from Telegram", runtime, grant=grant)
    return json.loads(buffer.getvalue())


def test_an_approved_command_is_granted_not_merely_unopposed(bridge_module) -> None:
    """`allow` alone means this hook does not object.

    It is not the same as the tool being permitted: Antigravity's own
    permission layer still stops and asks, so somebody who has already answered
    on their phone gets asked again at the desk — which is the thing Halyard
    exists to remove. `permissionOverrides` is documented as the way to
    override default tool permissions, and it is what closes that gap.
    """
    answer = emitted(bridge_module, "allow", "antigravity", grant="git push")

    assert answer["permissionOverrides"], "an allow with no grant is a second prompt"


def test_the_grant_names_the_command_exactly_as_it_is(bridge_module) -> None:
    """A literal, not a pattern. Four live runs, one success.

    `command(echo halyard-override-1)` was granted; the same shape with an
    escaped full stop was not, an absence was not, and `command(*)` was not.
    Every failure was a string that differed from the command.
    """
    answer = emitted(bridge_module, "allow", "antigravity", grant="echo halyard-override-1")

    assert "command(echo halyard-override-1)" in answer["permissionOverrides"]


def test_nothing_about_the_command_is_rewritten(bridge_module) -> None:
    """Brackets, quotes, full stops and non-ASCII all go through untouched.

    Escaping them, and then replacing them with a wildcard, were both attempts
    to be clever about a field that wants the command as it is — and each cost
    a live run and a second prompt for somebody who had already answered.
    """
    command = 'echo "a (b). ç"'

    answer = emitted(bridge_module, "allow", "antigravity", grant=command)

    assert f"command({command})" in answer["permissionOverrides"]
    assert f"unsandboxed({command})" in answer["permissionOverrides"]


def test_the_sandbox_form_is_still_sent_though_it_is_ignored(bridge_module) -> None:
    """Kept deliberately, and pinned so it is not tidied away.

    Antigravity ignores an `unsandboxed` override from a hook by design —
    honouring one would let whoever controls the hook run unsandboxed code on
    the machine. The shape is right and only the policy refuses it, so this is
    the line that starts working if that policy ever gains a way to say yes.
    Establishing the spelling took six live runs; rediscovering it should not
    be the price of a cleanup.
    """
    granted = emitted(bridge_module, "allow", "antigravity", grant="whoami")["permissionOverrides"]

    assert granted == ["command(whoami)", "unsandboxed(whoami)"]


def test_a_denial_grants_nothing(bridge_module) -> None:
    """Obvious, and worth a test precisely because it is: a deny that also
    handed out a permission would read as a refusal and act as consent."""
    answer = emitted(bridge_module, "deny", "antigravity", grant="git push")

    assert "permissionOverrides" not in answer


def test_no_grant_is_offered_when_there_is_no_command(bridge_module) -> None:
    """An empty override is still an override, and what it would grant is
    unknowable from here."""
    answer = emitted(bridge_module, "allow", "antigravity", grant=None)

    assert "permissionOverrides" not in answer


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_the_other_runtimes_never_see_that_field(bridge_module, runtime: str) -> None:
    """The measured rule, again: a payload that tries to serve every runtime
    serves none. An unrecognised key here does not get ignored — it stopped the
    whole answer being understood, and an unreadable answer is an approval."""
    answer = emitted(bridge_module, "allow", runtime, grant="git push")

    assert "permissionOverrides" not in answer
    assert "hookSpecificOutput" in answer


def test_the_grant_is_never_persisted_by_us(bridge_module) -> None:
    """Stated here because the whole scoping argument rests on it.

    The grant is only momentary because Antigravity forgets it after the call.
    Measured: a command granted this way never reached
    `~/.gemini/config/projects/<id>.json`, where the permissions somebody
    actually gave it are kept. Nothing in Halyard writes there, and nothing
    should start.
    """
    answer = emitted(bridge_module, "allow", "antigravity", grant="echo hello")

    assert set(answer) == {"decision", "reason", "permissionOverrides"}
