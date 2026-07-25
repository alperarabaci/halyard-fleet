"""The Claude Code adapter."""

from halyard.agents.claude_code.runner import ClaudeCodeRunner
from halyard.agents.claude_code.sessions import SessionRef, find_session, list_named_sessions
from halyard.agents.spec import Hooks, RuntimeSpec, Verification, late


def _present() -> bool:
    """The CLI, wherever it actually is.

    `which claude` is not the question. Claude Code ships its binary inside
    `claude.app`, and on a machine with no shim on `PATH` a `which` reports it
    missing — which is how a Mac mini configured with two Claude Code seats
    had them skipped while Antigravity, which does publish a command, was
    wired into the same project instead.
    """
    from halyard.agents.claude_code.runner import find_claude_binary

    return find_claude_binary() is not None


def _runner(settings=None) -> ClaudeCodeRunner:
    """Built from settings when there are any.

    The model list and the binary path are Claude Code's own settings, and
    reading them here keeps `create_app` from having to know that this one
    runtime takes three arguments and the others take none.
    """
    if settings is None:
        return ClaudeCodeRunner()
    models = tuple(m.strip() for m in (settings.claude_models or "").split(",") if m.strip())
    return ClaudeCodeRunner(
        binary=settings.claude_binary,
        models=models or None,
        default_model=(settings.claude_default_model or "").strip() or None,
    )


def _check_available(claude_binary=None, **_) -> list[tuple[str, str]]:
    """The CLI, which one, and whether it can sign in.

    The last of those is what a Mac mini was missing while looking healthy:
    the binary was there, doctor said so, and every delivery failed with the
    CLI's own "Not logged in · Please run /login".
    """
    from halyard.agents.claude_code.runner import find_claude_binary, signed_in

    found = find_claude_binary(claude_binary)
    if found is None:
        return [("fail", "the claude CLI is not on this machine")]

    lines = [("ok", f"messages use {found}")]
    match signed_in(claude_binary):
        case False:
            lines.append(("fail", "that CLI is not signed in, so nothing can be delivered"))
            # Quoted, because the path that gets printed here is usually
            # `~/Library/Application Support/...` — an instruction with a
            # space in it that cannot be pasted is not an instruction. And
            # `claude` is often absent from PATH even where it is installed:
            # the binary lives inside the desktop app's bundle, which is how
            # Halyard finds it and why the shell does not.
            lines.append(("", f'run this once on this machine:  "{found}" auth login'))
        case None:
            # Asked and not answered. Said quietly rather than as a failure —
            # an old CLI without `auth status` is not a broken installation.
            lines.append(("warn", "could not tell whether that CLI is signed in"))
    return lines


#: What the registry finds. See `halyard.agents.spec`.
#:
#: `.claude/settings.local.json` is not Halyard's file — Claude Code appends to
#: `permissions.allow` there every time somebody says "don't ask again" — which
#: is why wiring merges into it rather than writing it, and takes a copy first.
RUNTIME = RuntimeSpec(
    name="claude-code",
    human="Claude Code",
    binary="claude",
    hooks=Hooks(
        settings=".claude/settings.local.json",
        also=(".claude/settings.json",),
        matcher="Bash",
    ),
    runner=_runner,
    find_session=late("halyard.agents.claude_code", "find_session"),
    list_sessions=late("halyard.agents.claude_code", "list_named_sessions"),
    sessions_hint="`halyard sessions`",
    check_available=_check_available,
    present=_present,
    verify=Verification(
        command=("-p", "--model", "haiku", "{prompt}"),
        hook_timeout=5,
        matcher="Bash",
        # Allow-listed on purpose: without it, Claude Code's own permission
        # flow stops the command in headless mode and every case reads as a
        # pass. Measured, after an earlier version of the check was wrong for
        # exactly that reason.
        settings_extra=lambda marker: {"permissions": {"allow": [f"Bash(touch {marker})"]}},
    ),
)

__all__ = [
    "RUNTIME",
    "ClaudeCodeRunner",
    "SessionRef",
    "find_session",
    "list_named_sessions",
]
