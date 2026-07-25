"""Antigravity as an agent runtime."""

import shutil

from halyard.agents.antigravity.runner import (
    AntigravityRunner,
    find_antigravity_binary,
    language_server_endpoints,
)
from halyard.agents.antigravity.sessions import find_session, list_named_sessions
from halyard.agents.base import SessionRef
from halyard.agents.spec import Hooks, RuntimeSpec, late


def _present() -> bool:
    """The application or its CLI, either of which is worth wiring for.

    A PATH lookup is the wrong question here. Antigravity bundles its binaries
    inside `Antigravity.app` and puts nothing on PATH, so `which` reports it
    missing on the one machine where it is certainly installed — and this is
    the runtime that cannot be reached any other way.
    """
    return find_antigravity_binary() is not None or bool(shutil.which("agy"))


def _runner(settings=None) -> AntigravityRunner:
    """Takes nothing from settings; the argument is the shared shape."""
    return AntigravityRunner()


def _check_available(**_) -> list[tuple[str, str]]:
    """Installed, and open.

    The second half is a warning rather than a failure: the gate works whether
    or not the application is running, and only delivery needs it. Said out
    loud because this is the one runtime with no CLI to fall back on, so a
    closed application means messages simply do not arrive.
    """
    if find_antigravity_binary() is None:
        return [("fail", "Antigravity is not installed on this machine")]
    if not language_server_endpoints():
        return [("warn", "Antigravity is not running, so nothing can be sent")]
    return []


def _check_session(ref, **_) -> list[tuple[str, str]]:
    """Findable and unreachable, which is the pair worth printing.

    A conversation the `agy` CLI owns resolves perfectly — the name is right,
    the seat looks configured — and every message to it is refused, because the
    CLI keeps a separate store the application cannot see and
    `agy --conversation` starts a new conversation rather than continuing the
    one named.
    """
    from halyard.agents.antigravity.sessions import is_cli

    if not is_cli(ref.session_id):
        return []
    return [
        ("fail", "that conversation belongs to the agy CLI, not the app,"),
        ("", "and the CLI keeps a separate store the application"),
        ("", "cannot see. Nothing can be sent to it — reopen the"),
        ("", "work in the Antigravity application and name that."),
    ]


#: What the registry finds. See `halyard.agents.spec`.
#:
#: The third spelling of one idea: Claude Code says `Bash`, Codex says `Bash`
#: or `exec`, Antigravity says `run_command`.
#:
#: Its settings document is shaped differently from both. Every top-level key
#: is a hook *name* rather than an event, so the file is a namespace and two
#: tools can gate one project without either knowing about the other — and
#: only the tool events wrap their handlers in a `matcher`/`hooks` group. A
#: `Stop` written in the grouped shape has no `command` where Antigravity looks
#: for one, so the relay never runs while everything reports itself wired.
#:
#: `PreInvocation` is Antigravity-only, and it is how a person gets a word in.
#: It is the one hook that answers with `injectSteps`, and a
#: `{"userMessage": ...}` step is the only way text enters a conversation as a
#: turn somebody typed — `agentapi send-message` can file a `SYSTEM_MESSAGE`
#: and nothing else.
RUNTIME = RuntimeSpec(
    name="antigravity",
    human="Antigravity",
    binary="agy",
    prefix="g",
    hooks=Hooks(
        settings=".agents/hooks.json",
        matcher="run_command",
        dialect="named",
        extra=(("PreInvocation", None, "inject.py", 15),),
        grouped=("PreToolUse", "PostToolUse"),
        disableable=True,
    ),
    runner=_runner,
    find_session=late("halyard.agents.antigravity", "find_session"),
    list_sessions=late("halyard.agents.antigravity", "list_named_sessions"),
    present=_present,
    sessions_hint="the conversation names in the Antigravity application",
    check_available=_check_available,
    check_session=_check_session,
)

__all__ = [
    "RUNTIME",
    "AntigravityRunner",
    "SessionRef",
    "find_antigravity_binary",
    "find_session",
    "language_server_endpoints",
    "list_named_sessions",
]
