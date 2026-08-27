"""`halyard doctor` — answer "why is everything being denied" in one command.

Every piece of this system fails closed, which is correct and which makes a
misconfiguration look exactly like a working system refusing you. A bridge
pointed at the wrong address denies every command with a message about a port,
and there is no error anywhere to find.

So this walks the same path a hook does and says which step broke — and where
each setting came from, because "unreachable at 8787" is not useful on its own.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from halyard.config import Settings

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

OK = "  ok    "
WARN = "  warn  "
FAIL = "  FAIL  "


def _bridge_settings():
    sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import _settings

        return _settings
    except ImportError:
        return None
    finally:
        sys.path.pop(0)


def _source_of(settings_module, key: str) -> tuple[str | None, str]:
    if os.environ.get(key):
        return os.environ[key], "the environment"
    for path in settings_module._CONFIG_FILES:
        value = settings_module._read_key(path, key)
        if value:
            return value, str(path)
    return None, ""


def _resolved_url(settings_module) -> tuple[str, str]:
    """The address a bridge will use, and how it arrived at it."""
    explicit, where = _source_of(settings_module, "HALYARD_URL")
    if explicit:
        return explicit, f"HALYARD_URL, set in {where}"
    bind, where = _source_of(settings_module, "HALYARD_BIND")
    if bind:
        return settings_module._url_from_bind(bind), f"derived from HALYARD_BIND in {where}"
    return settings_module.DEFAULT_URL, "the built-in default — nothing is configured"


def project_root(directory: Path) -> Path:
    """Where Claude Code will look for `.claude/settings.json`.

    Not the directory the session is sitting in. Measured: a session opened in
    a subdirectory picks up hooks from `.claude/` at the **repository root**,
    and does not when there is no repository — with no `.git` above it, a
    parent's hooks never fire.

    That distinction is the whole reason this exists. A monorepo where the web
    app lives under the backend has one `.claude/` at the top gating every
    session inside it, and a checker that only looked at the session's own
    directory reported that nothing was gating a project that was fully gated.
    Being told a gate is missing when it is not is worse than not checking:
    the obvious response is to go and wire a second one.
    """
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


def _read(settings_file: Path) -> dict:
    try:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _disabled_hooks(settings_file: Path) -> list[str]:
    """The named hooks in a file that are switched off in place.

    `"enabled": false` leaves the file looking exactly like a wired one — the
    events are there, the commands are there, the paths are right — and runs
    none of it. That is the shape of failure this whole checker exists for.

    Only asked of runtimes whose spec says a hook can be disabled this way.
    """
    return [
        name
        for name, spec in _read(settings_file).items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    ]


def _named_hook_commands(settings_file: Path, grouped: tuple[str, ...]) -> list[tuple[str, str]]:
    """Every hook command in a `named`-dialect file, as (event, path).

    A different shape from the wrapped one: top-level keys are hook *names*,
    and only some events put their handlers behind a `matcher`/`hooks` group.
    Reading it with the other parser finds nothing at all and reports an
    ungated project, whose obvious remedy is to wire a second gate on top of
    the one already there.
    """
    from halyard.wiring import named_handlers

    found: list[tuple[str, str]] = []
    for document in _read(settings_file).values():
        if not isinstance(document, dict):
            continue
        for event in [key for key in document if key != "enabled"]:
            for handler in named_handlers(document, event, grouped):
                command = handler.get("command")
                if isinstance(command, str) and command.strip():
                    found.append((event, command.split()[0]))
    return found


def _hook_commands(
    settings_file: Path, project_dir: Path, runtime: str | None = None
) -> list[tuple[str, str]]:
    """Every hook command in a settings file, as (event, resolved path).

    Which parser to use is the runtime's own answer, not a name checked here.
    """
    from halyard.agents import registry

    spec = registry.get(runtime or registry.DEFAULT)
    if spec is not None and spec.hooks.dialect == "named":
        return _named_hook_commands(settings_file, spec.hooks.grouped)
    config = _read(settings_file)
    found: list[tuple[str, str]] = []
    for event, groups in (config.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group or {}).get("hooks") or []:
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                resolved = command.replace("$CLAUDE_PROJECT_DIR", str(project_dir)).replace(
                    "${CLAUDE_PROJECT_DIR}", str(project_dir)
                )
                found.append((event, resolved.split()[0] if resolved else ""))
    return found


def _travels_between_machines(settings_file: Path) -> str | None:
    """Why this hooks file will follow the repository onto another machine.

    A hooks file holds absolute paths — `/Users/you/checkout/bridge/hook.sh` —
    so it describes one machine and nothing else. Committed, it arrives on the
    next machine naming a home directory that does not exist there, and that
    machine's `wire` appends its own entry beside the dead one. Measured on a
    real project: two `PreToolUse` groups on one matcher, two `Stop` groups,
    two `PermissionRequest` groups, half of them pointing nowhere.

    `wire` cleans that up now, on every machine, every time the file comes
    back. Ignoring it fixes the cause instead — which is what this repository
    does with its own.

    A warning, never a failure: a team that has decided to commit theirs, with
    one shared checkout path, is not doing anything wrong.
    """
    import subprocess

    directory = settings_file.parent
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", settings_file.name],
            cwd=directory,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if tracked.returncode == 0:
        return "committed"

    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", settings_file.name],
            cwd=directory,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # 0 ignored, 1 not ignored, 128 not a repository at all — and a file
    # outside a repository cannot travel this way.
    return "not ignored" if ignored.returncode == 1 else None


def _render(check, label: str, *, indent: bool = False, **context) -> tuple[list[str], bool]:
    """Turn a runtime's own findings into doctor's lines, and say if it is fatal.

    The runtime answers `(level, text)`; how that looks on a terminal is this
    module's business and none of its own. A `fail` stops the seat being
    checked further, because everything after it would be a question asked of
    something that cannot answer.
    """
    if check is None:
        return [], False
    prefixes = {"ok": OK, "warn": WARN, "fail": FAIL}
    lines: list[str] = []
    fatal = False
    for level, text in check(**context):
        fatal = fatal or level == "fail"
        if not level:
            # A continuation line, already spoken for by the one above it.
            lines.append(f"{'':16}{text}")
            continue
        head = prefixes[level]
        lines.append(f"{head}{'        ' if indent else ''}{label}{': ' if label else ''}{text}")
    return lines, fatal


def _check_seat(
    seat,
    claude_binary: str | None = None,
    project_path: Path | None = None,
    claude_oauth_token: str | None = None,
) -> tuple[list[str], int]:
    """Everything one seat needs, checked against the runtime it actually is.

    Four seats and two runtimes means a name that resolves perfectly for one is
    absent from the other, and the mistake is easy to make because the names
    look alike. Asking the seat's own runtime is the only way to tell.
    """
    from halyard.agents import registry

    lines: list[str] = []
    label = f"{seat.label} ({seat.runtime})"

    if not seat.session:
        return [f"{WARN}{label}: no session name, so nothing can be sent to it"], 0

    spec = registry.get(seat.runtime)
    if spec is None:
        # A seat naming a runtime this build does not have. Said plainly rather
        # than skipped: it is a seat somebody believes in and cannot reach.
        return [f"{FAIL}{label}: no runtime package named {seat.runtime!r} is installed"], 1

    # Can it be reached at all, before asking it anything. The runtime answers;
    # this only renders. Each of these checks was a branch here until the
    # package that knows the answer took it back.
    reported, fatal = _render(
        spec.check_available,
        label,
        claude_binary=claude_binary,
        claude_oauth_token=claude_oauth_token,
    )
    lines += reported
    if fatal:
        return lines, 1

    ref = spec.find_session(seat.session)
    if ref is None:
        lines.append(f"{FAIL}{label}: no session named {seat.session!r}")
        lines.append(f"        {seat.runtime} does not know that name — check it against")
        lines.append(f"        {spec.sessions_hint or 'the names that runtime knows'}")
        return lines, 1

    lines.append(f"{OK}{label}: {seat.session}")
    reported, fatal = _render(spec.check_session, "", ref=ref, indent=True)
    lines += reported
    if fatal:
        return lines, 1
    if not ref.named_by_a_person:
        lines.append(f"{WARN}        that name was generated, and generated names are rewritten")
    if not seat.chat:
        lines.append(f"{WARN}        no chat, so it has nowhere of its own to speak")
    # Where the work happens. A session usually records it; an Antigravity one
    # never does — that lives in the running application, and the gate does not
    # need it because `workspacePaths` arrives with every hook call. So the
    # project's own `path:` stands in, which is the thing YAML added and the
    # reason a seat knows which project it belongs to.
    directory = ref.cwd or (str(project_path) if project_path else None)
    if not directory:
        lines.append(f"{WARN}        no directory recorded and the project has no `path:`,")
        lines.append("                so there is nothing to check the gate against")
        return lines, 0

    gate_lines, gate_problems = _check_gated_project(label, ref, seat, directory)
    return lines + gate_lines, gate_problems


def _check_gated_project(label: str, ref, seat, directory: str) -> tuple[list[str], int]:
    """Check the hooks wired into the project this seat's session works in.

    This is the check that would have caught an afternoon: settings copied from
    one machine to another still pointed at the first machine's paths, so the
    wrapper denied every command and nothing in the control plane knew why —
    the hook never reached it.

    Which file holds those hooks depends on the runtime. Looking in `.claude/`
    for a Codex seat would report a gate missing on a project that has one, and
    the obvious response to that is to go and wire a second.
    """
    lines: list[str] = []
    session_dir = Path(directory)
    project_dir = project_root(session_dir)
    lines.append(f"        {session_dir}")
    if project_dir != session_dir:
        lines.append(f"        gated from the repository root: {project_dir}")

    from halyard.agents import registry

    spec = registry.get(seat.runtime)
    # The runtime says where its hooks live. Claude Code is the one with two
    # candidates: a shared `settings.json` that is committed and a
    # `settings.local.json` that is not, and a gate may be written into either.
    settings_files = (
        [project_dir / name for name in spec.hooks.also] + [project_dir / spec.hooks.settings]
        if spec
        else []
    )
    present = [f for f in settings_files if f.exists()]
    if not present:
        where = spec.hooks.settings if spec else "a hooks file"
        lines.append(f"{FAIL}        no {where} — nothing is gating this project")
        lines.append(f"                halyard wire {project_dir}")
        return lines, 1

    problems = 0
    seen_events: set[str] = set()
    for settings_file in present:
        match _travels_between_machines(settings_file):
            case "committed":
                lines.append(f"{WARN}        {settings_file.name} is committed to this repository")
                lines.append("                it names absolute paths on this machine, so another")
                lines.append("                one gets hooks it cannot run. Add it to .gitignore.")
            case "not ignored":
                lines.append(f"{WARN}        {settings_file.name} is not in .gitignore")
                lines.append("                committing it sends this machine's paths to every")
                lines.append("                other checkout of this project.")
        switched_off = _disabled_hooks(settings_file) if spec and spec.hooks.disableable else []
        for name in switched_off:
            problems += 1
            lines.append(f'{FAIL}        "{name}" has "enabled": false, so none of it runs')
            lines.append("                the file below is wired and switched off")
        for event, command in _hook_commands(settings_file, project_dir, seat.runtime):
            seen_events.add(event)
            path = Path(command)
            if not path.exists():
                problems += 1
                lines.append(f"{FAIL}        {event} → {command}")
                lines.append("                that path does not exist on this machine")
            elif not path.stat().st_mode & 0o111:
                problems += 1
                lines.append(f"{FAIL}        {event} → {command} is not executable")
            else:
                elsewhere = path.resolve().parent != BRIDGE_DIR.resolve()
                note = "  (a different Halyard install)" if elsewhere else ""
                lines.append(f"{OK}        {event} → {path.name}{note}")

    if "PreToolUse" not in seen_events:
        problems += 1
        lines.append(f"{FAIL}        no PreToolUse hook — approvals will never be asked for")
    if "Stop" not in seen_events:
        lines.append(f"{WARN}        no Stop hook — replies will not reach the channel")

    if spec is not None and spec.check_wired is not None:
        # Whether these hooks will actually run is the runtime's own question.
        # Codex is the one that can be wired perfectly and still have no gate.
        reported, fatal = _render(
            spec.check_wired,
            "",
            indent=True,
            hooks_file=project_dir / spec.hooks.settings,
            project_dir=project_dir,
        )
        lines += reported
        problems += 1 if fatal else 0

    # Do not compare ref.started_at with the settings mtime. `started_at` is
    # when the *conversation was created*, not when the desktop/CLI process
    # most recently resumed it. A week-old conversation can have been reopened
    # after wiring five seconds ago. The old comparison therefore told people
    # to restart forever, including immediately after they had done so.
    #
    # Wire itself prints the necessary restart instruction at the only moment
    # we know settings changed. Doctor has no supported cross-runtime API for
    # observing the process that currently owns a desktop session.
    return lines, problems


#: When the service's own log is worth mentioning, and when it is worth acting
#: on. It holds everything the process printed to its console, so it repeats
#: much of the running log and grows for as long as the service does.
SERVICE_LOG_NOTE_BYTES = 20_000_000
SERVICE_LOG_WARN_BYTES = 100_000_000


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB"


def check_service_log(path: Path) -> tuple[list[str], int]:
    """Say something useful about launchd's copy of the console output.

    This one is not Halyard's to rotate. launchd opens it and holds it, so
    renaming it out from under a running service leaves the service writing to
    a file nobody can find — the quiet kind of wrong this project keeps writing
    down. Truncating in place is the safe move, and it is a person's to make.

    So: mention it exists, and say the number when the number starts to matter.
    Silence would leave a file growing all year that nothing ever names.
    """
    try:
        size = path.stat().st_size
    except OSError:
        # Not installed as a service, or somewhere this cannot look. Neither is
        # a problem — but the path is still worth printing once, because it is
        # where the `git pull` and `uv sync` output goes and nothing else says so.
        return (
            [
                f"{OK}no service log at {path}",
                "        the launchd service keeps its console output there when installed",
            ],
            0,
        )

    lines = [f"{OK}service log is {_megabytes(size)} ({path})"]
    if size >= SERVICE_LOG_WARN_BYTES:
        lines = [
            f"{WARN}service log has reached {_megabytes(size)} ({path})",
            "        launchd holds this open, so Halyard cannot rotate it — empty it in place:",
            f"        : > {path}",
        ]
        return lines, 1
    if size >= SERVICE_LOG_NOTE_BYTES:
        lines.append("        launchd's, not Halyard's. Empty it when it gets large:")
        lines.append(f"        : > {path}")
    return lines, 0


def run() -> int:
    """Check the chain end to end. Returns a process exit code."""
    problems = 0
    settings_ok = False
    print("Halyard doctor\n")

    try:
        settings = Settings()
        settings_ok = True
        print(f"{OK}configuration loads")
        print(f"        channel={settings.channel.value} project={settings.project_name!r}")
        print(
            f"{OK}timeouts ordered: approval {settings.approval_timeout_seconds}s"
            f" < bridge {settings.bridge_timeout_seconds}s"
            f" < hook {settings.hook_timeout_seconds}s"
        )
        if settings.channel.decides_without_a_human:
            print(f"{WARN}channel {settings.channel.value} answers by itself — nobody is asked")
    except ValidationError as exc:
        problems += 1
        print(f"{FAIL}configuration is not valid")
        for error in exc.errors():
            print(f"        {'.'.join(str(p) for p in error['loc']) or '?'}: {error['msg']}")

    bridge_settings = _bridge_settings()
    if bridge_settings is None:
        print(f"\n{FAIL}bridge/_settings.py could not be imported")
        return 1

    url, source = _resolved_url(bridge_settings)
    print(f"\n{OK}bridges will use {url}")
    print(f"        {source}")

    health: dict | None = None
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5) as response:
            health = json.loads(response.read())
        print(f"{OK}control plane answers there")
        print(
            f"        channel={health.get('channel')} project={health.get('project')!r}"
            f" open_approvals={health.get('open_approvals')}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        problems += 1
        print(f"{FAIL}nothing answering at {url} ({exc})")
        print("        Until this is reachable, every approval is denied.")
        print("        Start it with `uv run halyard` or `docker compose up -d`,")
        print("        and check HALYARD_BIND is the address you meant.")

    if health and health.get("decides_without_a_human"):
        print(f"{WARN}the running control plane answers approvals by itself")

    print()
    from halyard.core.config_file import find_config
    from halyard.core.seats import configured

    source = find_config()
    try:
        seats = configured()
    except ValueError as error:
        # A seat that will not parse is a seat you believe you have.
        print(f"{FAIL}seats: {error}")
        seats = []
        problems += 1

    if not seats:
        # Silence used to mean "checked and fine". It meant "checked nothing":
        # when seats replaced the two role settings this stopped looking at
        # anything and still printed a clean bill of health.
        print(f"{WARN}no seats configured — nothing is being routed anywhere")
        print("        run `halyard init`, or add a `projects:` block to halyard.yaml")
    else:
        # Which file the seats came from, because the two dialects do not merge
        # and a file left behind would otherwise silently outrank what was
        # somebody had just edited.
        print(f"{OK}seats read from {source if source else 'the environment'}")
        for project in sorted({seat.project for seat in seats if seat.project}):
            labels = ", ".join(s.label for s in seats if s.project == project)
            print(f"        {project}: {labels}")

    from halyard.core.config_file import projects as described_projects

    try:
        where = {p.name: p.path for p in described_projects()}
    except ValueError:
        where = {}

    for seat in seats:
        lines, found = _check_seat(
            seat,
            settings.claude_binary,
            where.get(seat.project),
            settings.claude_oauth_token,
        )
        problems += found
        for line in lines:
            print(line)
    if seats:
        print()

    for name, required in (
        ("hook.sh", True),
        ("permission_hook.sh", True),
        ("hook_bridge.py", True),
        ("relay.py", False),
    ):
        path = BRIDGE_DIR / name
        if not path.exists():
            problems += required
            print(f"{FAIL if required else WARN}{path} is missing")
        elif not path.stat().st_mode & 0o111:
            problems += required
            print(f"{FAIL if required else WARN}{name} is not executable (chmod +x)")
        else:
            print(f"{OK}{name} is present and executable")

    print()
    if settings_ok and settings.log_file is not None:
        folder = settings.log_file.parent
        weeks = sorted(folder.glob("bridge-*.log")) if folder.is_dir() else []
        print(f"{OK}logs are in {folder}, a new file each week")
        print(
            f"        {settings.log_file.name} is this week"
            + (f", and {len(weeks)} week(s) of bridge log beside it" if weeks else "")
        )

    from halyard.service import log_path as service_log_path

    lines, found = check_service_log(service_log_path())
    problems += found
    for line in lines:
        print(line)

    print()
    print(f"{problems} problem(s) found." if problems else "Everything checks out.")
    return 1 if problems else 0


def sessions() -> int:
    """List the session names this machine can see, newest first.

    Exists so the names are copied rather than guessed. They have to match
    exactly, and a name typed from memory that is nearly right routes nothing
    and explains nothing.

    Asked of every runtime rather than of Claude Code. This read `~/.claude`
    directly for a long time, which meant a machine with Codex seats was told
    it had no sessions — and `tests/test_runtime_isolation.py` now fails if any
    module outside a runtime's own package names one or knows where it lives.

    Read on the host, not in the container: transcripts live in the user's home
    directory, which the control plane cannot see.
    """
    from halyard.agents import registry

    found: list[tuple[object, str]] = []
    for name, spec in sorted(registry.discover().items()):
        try:
            found += [(ref, name) for ref in spec.list_sessions()]
        except Exception:
            # One runtime that cannot list must not hide the others.
            print(f"{WARN}could not list {name} sessions")

    if not found:
        print("No named sessions found on this machine.")
        return 1

    found.sort(key=lambda item: -(item[0].last_active.timestamp() if item[0].last_active else 0.0))

    print("Session names visible on this machine, newest first:\n")
    generated = False
    for ref, runtime in found:
        when = ref.last_active.strftime("%Y-%m-%d %H:%M") if ref.last_active else " " * 16
        generated = generated or not ref.named_by_a_person
        mark = "" if ref.named_by_a_person else "   ⚠ auto-titled"
        print(f"  {when}  {runtime:<12} {ref.name}{mark}")
        # Said even when absent. A blank line reads as "no directory", and a
        # seat pointed at a session whose directory nobody recorded fails in a
        # way that looks like the name being wrong.
        print(f"{'':20}{ref.cwd or '(directory not recorded)'}")
    print(
        "\nGive one to a seat, exactly as printed above:\n"
        "\n"
        "  seats:\n"
        "    drv:\n"
        "      runtime: <the runtime column>\n"
        "      session: <one of the names above>\n"
        '      chat: "-100..."      # the group this seat speaks in\n'
        "\n"
        "Seats are read at startup, so restart the control plane afterwards."
    )
    if generated:
        print(
            f"\n{WARN}A name marked auto-titled was written by the runtime, not by you.\n"
            "        Those are rewritten as a conversation moves, so a seat pointed at\n"
            "        one works today and silently stops later. Rename it first."
        )
    return 0
