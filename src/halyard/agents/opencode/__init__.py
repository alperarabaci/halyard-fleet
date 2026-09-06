"""opencode, as a runtime Halyard can gate.

The fourth, and the first that is not driven by hooks. Three things about it
were measured rather than read, and each one would have produced a gate that
looked installed and did nothing:

**Its gate is a plugin, not a hooks file.** A module in `.opencode/plugins/`,
loaded at startup with no config entry and no package install — and loaded in
the TUI, not only under `opencode serve`, because the TUI embeds the same
server. That matters because the TUI is how anybody actually uses it.

**Its permission hook is declared and never called.** `@opencode-ai/plugin`
1.18.29 has a `permission.ask` whose signature reads exactly like a gate. It
was measured against real approvals, twice, and it never fired. What arrives is
a `permission.asked` event, and the answer goes back over the HTTP API. So the
bridge here hears a question and sends an answer rather than returning a
verdict — see `bridge/opencode.ts`, where that difference is written down.

**It does not ask by default.** Fifteen turns and a file edited produced no
permission event at all. Without a `permission` block in the project's own
config the plugin is wired and dead, which is why wiring writes both.

Sessions have no names here — an id and a title the runtime writes itself,
from the content, changing as the content does. So a seat cannot be bound the
way the other three bind one, and `find_session` says so rather than guessing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from halyard.agents.base import SessionRef
from halyard.agents.opencode import wiring
from halyard.agents.spec import Hooks, RuntimeSpec

#: Long enough for a cold start, short enough that `doctor` stays answerable.
TIMEOUT = 10.0


def _binary() -> str | None:
    return shutil.which("opencode")


def _version(binary: str) -> str | None:
    try:
        done = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip().splitlines()[-1].strip() if done.returncode == 0 else None


def _available_binary() -> list[tuple[str, str]]:
    """Whether this machine has the CLI at all."""
    found = _binary()
    if not found:
        return [
            ("fail", "no opencode CLI on PATH, so nothing here can be gated or reached"),
            ("", "install it, then run `halyard doctor` again"),
        ]
    lines = [("ok", f"opencode at {found}")]
    if version := _version(found):
        lines.append(("", f"version {version}"))
    return lines


def check_wired(hooks_file: Path, project_dir: Path, **_context) -> list[tuple[str, str]]:
    """Whether this project's gate is present *and* switched on.

    Two questions, and the second is the one worth asking. A plugin sitting in
    the right directory proves nothing on its own: the runtime asks about
    nothing unless its own configuration tells it to, so a project can be
    wired, loaded and silent. That was measured before any of this was written.
    """
    lines: list[tuple[str, str]] = []

    if not hooks_file.is_file():
        return [
            ("fail", f"no {wiring.PLUGINS / wiring.PLUGIN} — nothing is gating this project"),
            ("", f"halyard wire {project_dir}"),
        ]
    lines.append(("ok", f"{wiring.PLUGINS / wiring.PLUGIN} is in place"))

    config = project_dir / wiring.CONFIG
    try:
        document = wiring._read(config)
    except (OSError, ValueError):
        return [
            *lines,
            ("fail", f"{wiring.CONFIG} could not be read, so what it asks about is unknown"),
        ]

    permission = document.get("permission")
    permission = permission if isinstance(permission, dict) else {}
    silent = sorted(name for name, how in wiring.ASK.items() if permission.get(name) != how)
    if silent:
        lines.append(("fail", f"{wiring.CONFIG} does not ask about {', '.join(silent)}"))
        lines.append(("", "the plugin is loaded and will never be consulted about those"))
        lines.append(("", f"halyard wire {project_dir}"))
    else:
        lines.append(("ok", f"{wiring.CONFIG} asks about {', '.join(sorted(wiring.ASK))}"))
    return lines


def reachable(port: int | None) -> tuple[bool, str]:
    """Whether a server is answering on this port, and what it said.

    Nothing else in this package needs the network. This does, because the
    thing it is checking for cannot be seen any other way: the gate looks
    perfectly wired and delivers questions it can never answer.
    """
    if not port:
        return False, "no port configured"
    import urllib.error
    import urllib.request

    try:
        # A literal loopback URL, built here rather than taken from anywhere.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=3) as answered:
            return answered.status == 200, f"answered {answered.status}"
    except urllib.error.HTTPError as refused:
        # Answering at all is what is being tested. A 404 means something is
        # listening, which is the question.
        return True, f"answered {refused.code}"
    except Exception as unreachable:
        return False, str(unreachable)


def check_available(**_context) -> list[tuple[str, str]]:
    """Whether this machine can run it, and whether it can be reached.

    **The second half is not optional here.** This runtime's gate answers an
    approval by calling back into opencode's own HTTP API, and that API is
    served only when the TUI is started with `--port`. Its default is to serve
    nothing reachable — measured: `opencode` alone leaves a process listening
    on no TCP port at all, and `opencode --port 4096` answers immediately.

    A gate in that state is the worst shape this project knows: the question
    reaches the phone, the button does nothing, and every part of `wire` and
    every file on disk says the project is gated.
    """
    lines = list(_available_binary())
    if any(level == "fail" for level, _ in lines):
        return lines

    from halyard.core.config_file import runtime_settings

    try:
        configured = runtime_settings().get("opencode")
    except Exception:
        # A configuration that will not parse is somebody else's report to
        # make; this check is not the place to raise it a second time.
        configured = None
    port = configured.port if configured else None

    if not port:
        lines.append(("warn", "no `runtimes: opencode: port:` in halyard.yaml"))
        lines.append(("", "without one nothing here can tell whether the gate can answer"))
        lines.append(("", "add `port: 4096`, and start it with `opencode --port 4096`"))
        return lines

    answering, said = reachable(port)
    if answering:
        lines.append(("ok", f"opencode is answering on port {port}"))
    else:
        lines.append(("warn", f"nothing is answering on port {port} ({said})"))
        lines.append(("", "that is normal when it is not running. When it is, start it with"))
        lines.append(("", f"`opencode --port {port}` — without it the gate can ask and"))
        lines.append(("", "never answer, and everything else will look correct"))
    return lines


def find_session(name: str) -> SessionRef | None:
    """Nothing, always — and honestly rather than by failing to look.

    Sessions here have an id and a title the runtime writes from the content of
    the conversation. Neither is a name somebody chose, and the title changes as
    the work does, so matching on it would bind a seat to a session that stops
    being that session. A seat for this runtime is bound to its project instead.
    """
    return None


def list_sessions() -> list[SessionRef]:
    """Nothing to offer `halyard init`, for the same reason."""
    return []


RUNTIME = RuntimeSpec(
    name="opencode",
    human="opencode",
    binary="opencode",
    prefix="o",
    hooks=Hooks(
        # Not a hooks file. `settings` is what core reads when it needs to name
        # the file in the project that carries this runtime's gate, and for
        # this one that is the plugin. Nothing writes it through the hooks
        # path: `install` below is what puts it there.
        settings=str(wiring.PLUGINS / wiring.PLUGIN),
        matcher="|".join(sorted(wiring.ASK)),
        dialect="plugin",
    ),
    runner=lambda *_args, **_kwargs: None,
    find_session=find_session,
    list_sessions=list_sessions,
    sessions_hint="opencode keeps sessions per project, and names none of them",
    check_available=check_available,
    check_wired=check_wired,
    install=wiring.install,
    uninstall=wiring.uninstall,
    when_unanswered=(
        "nothing is denied and nothing is let through. This runtime has already\n"
        "  put the question on its own screen and is waiting there; a control plane\n"
        "  that cannot answer just leaves it for whoever is at the desk. So the\n"
        "  project keeps working with Halyard down — you answer it yourself."
    ),
)
