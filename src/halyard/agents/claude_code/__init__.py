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
        oauth_token=settings.claude_oauth_token,
    )


def _check_available(claude_binary=None, claude_oauth_token=None, **_) -> list[tuple[str, str]]:
    """The CLI, which one, and whether it can sign in.

    The last of those is what a Mac mini was missing while looking healthy:
    the binary was there, doctor said so, and every delivery failed with the
    CLI's own "Not logged in · Please run /login".
    """
    import os

    from halyard.agents.claude_code.runner import auth_method, find_claude_binary, signed_in

    found = find_claude_binary(claude_binary)
    if found is None:
        return [("fail", "the claude CLI is not on this machine")]

    lines = [("ok", f"messages use {found}")]

    # Which credential, not just whether there is one. A control plane running
    # on the login somebody made at the keyboard works until that login expires
    # — measured twice, four days apart, each time stopping remote work with
    # "OAuth session expired and could not be refreshed" until somebody was
    # back at the desk. `auth status` carries no expiry to warn from, so the
    # useful thing to say is which credential is in use and what that implies.
    if claude_oauth_token:
        lines.append(("ok", "turns from here use a long-lived token"))
        if os.environ.get("ANTHROPIC_API_KEY"):
            lines.append(
                ("warn", "ANTHROPIC_API_KEY outranks that token, and bills the API not the plan")
            )
    else:
        lines.append(
            ("warn", "turns from here use the desktop login, which expires and stops deliveries")
        )
        # Said only here, where somebody needs to know what it is falling back
        # to. A clean check stays one line rather than a paragraph about what
        # is already fine.
        if method := auth_method(claude_binary):
            lines.append(("", f"the CLI reports authMethod={method}"))
        lines.append(("", f'mint one that lasts a year:  "{found}" setup-token'))
        lines.append(("", "then set HALYARD_CLAUDE_OAUTH_TOKEN in halyard.yaml"))

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
        # `AskUserQuestion` rides the same `PreToolUse` hook as the shell tool,
        # so one matcher covers both and the wiring keeps its one-hook-per-event
        # shape — a second group under `PreToolUse` sharing `hook.sh` would look
        # to the wiring like the gate already installed and quietly rewrite the
        # gate's own matcher. The bridge tells the two apart by tool name and
        # sends the question down a path that fails *open*: unanswered, it goes
        # back to the terminal picker rather than being denied.
        settings=".claude/settings.local.json",
        also=(".claude/settings.json",),
        # `Write` and `Edit` are here because of a failure the gate could not
        # see. A turn started from a phone runs headless, where Claude Code
        # cannot open a permission dialog — so a write that is not on its own
        # allow list was denied outright, with no card and nothing to answer.
        # Work stopped mid-sentence and the reason was invisible from away.
        #
        # The cost is stated rather than hidden: these tools now need Halyard
        # running, exactly as Bash does, and each unlisted write asks. The
        # `writes:` block in halyard.yaml is how you say which paths should not
        # have to — see `core/writes.py`.
        matcher="Bash|AskUserQuestion|Write|Edit|MultiEdit|NotebookEdit",
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
