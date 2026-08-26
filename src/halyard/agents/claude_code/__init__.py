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
        settings=".claude/settings.local.json",
        also=(".claude/settings.json",),
        # Everything gated rides this one `PreToolUse` hook, so one matcher
        # covers them all and the wiring keeps its one-hook-per-event shape — a
        # second group sharing `hook.sh` would look to the wiring like the gate
        # was already installed and quietly rewrite the gate's own matcher. The
        # bridge tells them apart by tool name.
        #
        # Each addition after `Bash` closed a silence somebody hit in the field.
        # A turn started from a phone runs headless, where Claude Code cannot
        # open a permission dialog: at the desk an ungranted tool is a popup,
        # and from away it was denied outright, with no card and nothing to
        # answer. `Write`/`Edit` stopped work mid-sentence that way; MCP calls
        # and `WebFetch` were the same failure a week later.
        #
        # `AskUserQuestion` is the exception in kind: it fails *open*, back to
        # the terminal picker, because a question nobody answers is not
        # dangerous the way an unapproved command is.
        #
        # Deliberately a list rather than `.*`. Measured with a passive hook:
        # `Read` and `ToolSearch` fire too, so gating everything would put a
        # card in front of every file read and every tool the client loads.
        #
        # The cost is stated rather than hidden: all of these now need Halyard
        # running, exactly as Bash does. `writes:` and `tools:` in halyard.yaml
        # are how you say which of them should not have to ask.
        matcher=(
            "Bash|AskUserQuestion|Write|Edit|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__.*"
        ),
        # `SessionStart` fires when a session opens and again once a compaction
        # has finished, told apart by `source`. The second is where a seat gets
        # its orientation back — measured to be the only injection point around
        # a compaction that reaches the model at all.
        extra=(
            # Generous, because this one blocks: the record is written while
            # the compaction waits. The control plane gives up at 120s, and a
            # hook that outruns its own timeout is discarded — which here means
            # the summary proceeds without a record, the safe direction.
            ("PreCompact", None, "compaction.py", 180),
            ("SessionStart", None, "compaction.py", 15),
        ),
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
