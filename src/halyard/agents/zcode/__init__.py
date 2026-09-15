"""The ZCode adapter.

ZCode is a desktop application with a coding agent inside it, and its hooks are
Claude Code's in all but three respects — each measured on 3.11.2 with a probe
workspace that wrote down every call. See `docs/zcode-payload-notes.md`.

- **The file.** Hooks live in the workspace's `.zcode/config.json` under
  `hooks.events`, and none of them run unless `hooks.enabled` is true.
- **Trust.** A workspace's hooks wait until somebody trusts them in ZCode, one
  declaration at a time, and a changed declaration waits again. See `trust`.
- **Names.** A call carries only a `sess_<uuid>`, new with every task. The title
  a session shows in ZCode is in the application's own database, where the
  bridge and `sessions` read it, so a seat names its session as it would on any
  other runtime; a seat without a name is found by its project, as opencode's is.

The payload itself is Claude Code's, with camelCase copies of every field beside
the snake_case ones. Nothing in a call says which runtime made it — its
transcript is a temporary copy in no runtime's home — so the hooks this package
writes say it for themselves. See `wiring`.

Delivery is not here yet. Nothing outside the application is known to put a
message into a ZCode session, so a seat on this runtime gets its approvals and
its replies on the phone and takes new instructions at the desk.
"""

from __future__ import annotations

from halyard.agents.spec import Hooks, RuntimeSpec
from halyard.agents.zcode import sessions, trust, wiring
from halyard.agents.zcode.runner import ZCodeRunner


def _present() -> bool:
    """The application, wherever macOS keeps it. It puts nothing on PATH."""
    return trust.app() is not None


def _runner(settings=None) -> ZCodeRunner:
    return ZCodeRunner()


def check_available(**_context) -> list[tuple[str, str]]:
    found = trust.app()
    if found is None:
        where = " or ".join(str(app.parent) for app in trust.APPS)
        return [("fail", f"no ZCode.app in {where}")]
    return [("ok", f"{found} is installed")]


RUNTIME = RuntimeSpec(
    name="zcode",
    human="ZCode",
    # Not on PATH: the application keeps its engine inside the bundle, and
    # `present` is what answers whether it is here.
    binary="zcode",
    prefix="z",
    hooks=Hooks(settings=str(wiring.CONFIG), matcher=wiring.MATCHER, dialect="events"),
    runner=_runner,
    find_session=sessions.find_session,
    list_sessions=sessions.list_sessions,
    sessions_hint=(
        "the titles in ZCode's own session list: rename the session there to match, "
        "since a title set by hand stays and one ZCode generated moves"
    ),
    present=_present,
    check_available=check_available,
    check_wired=trust.check_wired,
    install=wiring.install,
    uninstall=wiring.uninstall,
)
