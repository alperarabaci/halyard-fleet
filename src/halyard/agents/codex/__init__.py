"""Codex as an agent runtime."""

from halyard.agents.base import SessionRef
from halyard.agents.codex import trust
from halyard.agents.codex.runner import CodexRunner, find_codex_binary
from halyard.agents.codex.sessions import find_session, list_named_sessions
from halyard.agents.spec import Hooks, RuntimeSpec, Verification, late


def _present() -> bool:
    """Asked of the package that knows where Codex keeps itself."""
    return find_codex_binary() is not None


def _runner(settings=None) -> CodexRunner:
    """Takes nothing from settings; the argument is the shared shape."""
    return CodexRunner()


def _check_available(**_) -> list[tuple[str, str]]:
    from halyard.agents.codex.runner import find_codex_binary

    if find_codex_binary() is None:
        return [("fail", "the codex CLI is not on this machine")]
    return []


#: What the registry finds. See `halyard.agents.spec`.
#:
#: The matcher names four tools because Codex has two front ends and they do
#: not agree. `codex exec` runs a shell command as a tool the payload calls
#: `Bash`; the desktop app calls a tool named `exec` whose input is JavaScript,
#: with the shell call inside it. Measured from one session driven both ways: a
#: `function_call` named `exec_command` from the CLI, a `custom_tool_call`
#: named `exec` from the app. `^Bash$` gated the CLI, ignored the app, and
#: reported itself installed the whole time.
#:
#: `PermissionRequest` is Codex-only. It asks a separate native question when a
#: tool requests sandbox escalation, and a `PreToolUse` allow does not answer
#: it — where Claude Code's `PreToolUse` decision replaces its prompt outright.
RUNTIME = RuntimeSpec(
    name="codex",
    human="Codex",
    binary="codex",
    prefix="x",
    hooks=Hooks(
        settings=".codex/hooks.json",
        matcher="^(Bash|exec|exec_command|shell)$",
        extra=(("PermissionRequest", "Bash", "permission_hook.sh", 600),),
    ),
    runner=_runner,
    find_session=late("halyard.agents.codex", "find_session"),
    list_sessions=late("halyard.agents.codex", "list_named_sessions"),
    sessions_hint="the Codex thread names on this machine",
    check_available=_check_available,
    present=_present,
    check_wired=trust.check_wired,
    verify=Verification(
        # Sandbox, approvals and hook trust are all opened deliberately, and
        # only inside a throwaway directory the harness just created.
        #
        # It has to remove every blocker except the gate: a command stopped by
        # Codex's own approval flow looks exactly like one the gate stopped,
        # and reading that as a pass is how a project with no gate at all gets
        # a clean bill of health.
        #
        # Trust is the same problem in a form that cannot be worked around.
        # Those hooks are written seconds earlier, in a directory that will not
        # exist afterwards — so they have never been trusted and never can be,
        # and without the bypass every case is inconclusive forever. Approving
        # hooks in a real project does nothing for them.
        #
        # Not a hole: what trust protects against is a hook somebody else
        # wrote, and this one is written by the check itself. Whether a *real*
        # project's hooks are trusted is a different question, and
        # `halyard doctor` is what answers it.
        command=(
            "exec",
            "-m",
            "gpt-5.4-mini",
            "--skip-git-repo-check",
            "-s",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            "--dangerously-bypass-hook-trust",
            "{prompt}",
        ),
        hook_timeout=5,
        # The harness drives `codex exec` only, so it does not need the app's
        # `exec` spelling that the real gate matches.
        matcher="^Bash$",
        expects_hook_mention=True,
    ),
)

__all__ = [
    "RUNTIME",
    "CodexRunner",
    "SessionRef",
    "find_codex_binary",
    "find_session",
    "list_named_sessions",
]
