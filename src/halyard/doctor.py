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
from datetime import datetime
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


def _hook_commands(settings_file: Path, project_dir: Path) -> list[tuple[str, str]]:
    """Every hook command in a settings file, as (event, resolved path)."""
    try:
        config = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
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


def _check_seat(seat) -> tuple[list[str], int]:
    """Everything one seat needs, checked against the runtime it actually is.

    Four seats and two runtimes means a name that resolves perfectly for one is
    absent from the other, and the mistake is easy to make because the names
    look alike. Asking the seat's own runtime is the only way to tell.
    """
    lines: list[str] = []
    label = f"{seat.label} ({seat.runtime})"

    if not seat.session:
        return [f"{WARN}{label}: no session name, so nothing can be sent to it"], 0

    if seat.runtime == "codex":
        from halyard.agents.codex import find_codex_binary
        from halyard.agents.codex import find_session as look_up

        if find_codex_binary() is None:
            return [f"{FAIL}{label}: the codex CLI is not on this machine"], 1
    else:
        from halyard.agents.claude_code import find_session as look_up
        from halyard.agents.claude_code.runner import find_claude_binary

        if find_claude_binary() is None:
            return [f"{FAIL}{label}: the claude CLI is not on this machine"], 1

    ref = look_up(seat.session)
    if ref is None:
        lines.append(f"{FAIL}{label}: no session named {seat.session!r}")
        lines.append(f"        {seat.runtime} does not know that name — check it against")
        lines.append(
            "        `halyard sessions`"
            if seat.runtime == "claude-code"
            else "        the Codex thread names on this machine"
        )
        return lines, 1

    lines.append(f"{OK}{label}: {seat.session}")
    if not ref.named_by_a_person:
        lines.append(f"{WARN}        that name was generated, and generated names are rewritten")
    if not seat.chat:
        lines.append(f"{WARN}        no chat, so it has nowhere of its own to speak")
    if not ref.cwd:
        lines.append(f"{WARN}        no directory recorded, so nothing can be gated")
        return lines, 0

    gate_lines, gate_problems = _check_gated_project(label, ref, seat)
    return lines + gate_lines, gate_problems


def _check_gated_project(label: str, ref, seat) -> tuple[list[str], int]:
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
    session_dir = Path(ref.cwd)
    project_dir = project_root(session_dir)
    lines.append(f"        {session_dir}")
    if project_dir != session_dir:
        lines.append(f"        gated from the repository root: {project_dir}")

    settings_files = (
        [project_dir / ".codex" / "hooks.json"]
        if seat.runtime == "codex"
        else [
            project_dir / ".claude" / "settings.json",
            project_dir / ".claude" / "settings.local.json",
        ]
    )
    present = [f for f in settings_files if f.exists()]
    if not present:
        where = ".codex/hooks.json" if seat.runtime == "codex" else ".claude/settings.json"
        lines.append(f"{FAIL}        no {where} — nothing is gating this project")
        lines.append(f"                halyard wire {project_dir}")
        return lines, 1

    problems = 0
    newest_settings = max(f.stat().st_mtime for f in present)
    seen_events: set[str] = set()
    for settings_file in present:
        for event, command in _hook_commands(settings_file, project_dir):
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

    if seat.runtime == "codex":
        from halyard.wiring import codex_trust_is_stale, codex_untrusted

        hooks_file = project_dir / ".codex" / "hooks.json"
        # Codex accepts `description` and `hooks` and refuses a file containing
        # anything else — the whole file, not the field. It says so in a
        # warning on stderr and then behaves as though no hooks existed, which
        # is a gate that has silently ceased to exist on a project that looks
        # correctly wired. Measured: one stray key cost an evening.
        try:
            keys = set(json.loads(hooks_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            keys = set()
        stray = sorted(keys - {"description", "hooks"})
        if stray:
            problems += 1
            lines.append(f"{FAIL}        hooks.json has field(s) Codex rejects: {', '.join(stray)}")
            lines.append("                it refuses the whole file over one, so nothing is gated")
        if codex_untrusted(hooks_file):
            problems += 1
            lines.append(f"{FAIL}        hooks here have never been trusted")
            lines.append("                Codex skips an untrusted hook without a word, so this")
            lines.append("                project has no gate at all. Review and trust them:")
            lines.append(f"                    cd {project_dir} && codex")
            lines.append("                A hook review appears at startup. Prefer `Review hooks`")
            lines.append("                over `Trust all` — these run from outside the project.")
        elif codex_trust_is_stale(hooks_file):
            lines.append(f"{WARN}        hooks or bridge scripts changed since trust was recorded")
            lines.append("                Codex hashes the handler, so updating this checkout may")
            lines.append("                have revoked it — and a revoked hook is skipped in")
            lines.append(f"                silence. Re-review with: cd {project_dir} && codex")

    if ref.started_at and ref.started_at.timestamp() < newest_settings:
        # With the date, because the two are often a day apart and bare
        # clock times then read as though the warning had it backwards.
        changed = datetime.fromtimestamp(newest_settings).strftime("%b %d %H:%M")
        began = ref.started_at.astimezone().strftime("%b %d %H:%M")
        lines.append(f"{WARN}        settings changed at {changed}, session started at {began}")
        lines.append("                hooks are read at startup — restart it to pick them up")
    return lines, problems


def run() -> int:
    """Check the chain end to end. Returns a process exit code."""
    problems = 0
    print("Halyard doctor\n")

    try:
        settings = Settings()
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
    from halyard.core.seats import from_environment

    try:
        configured = from_environment()
    except ValueError as error:
        # A seat that will not parse is a seat you believe you have.
        print(f"{FAIL}seats: {error}")
        configured = []
        problems += 1

    if not configured:
        # Silence used to mean "checked and fine". It meant "checked nothing":
        # when seats replaced the two role settings this stopped looking at
        # anything and still printed a clean bill of health.
        print(f"{WARN}no seats configured — nothing is being routed anywhere")
        print("        set HALYARD_SEATS, or the navigator/driver pair, in .env")
    for seat in configured:
        lines, found = _check_seat(seat)
        problems += found
        for line in lines:
            print(line)
    if configured:
        print()

    for name, required in (("hook.sh", True), ("hook_bridge.py", True), ("relay.py", False)):
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
    print(f"{problems} problem(s) found." if problems else "Everything checks out.")
    return 1 if problems else 0


def sessions() -> int:
    """List the session names this machine can see, newest first.

    Exists so the two names in `.env` are copied rather than guessed. They have
    to match exactly, and a name typed from memory that is nearly right routes
    nothing and explains nothing.

    Read on the host, not in the container: transcripts live in the user's home
    directory, which the control plane cannot see.
    """
    from halyard.agents.claude_code.sessions import describe

    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        print(f"No transcripts found under {root}.")
        return 1

    seen: dict[str, tuple[float, str, bool]] = {}
    for transcript in root.glob("*/*.jsonl"):
        try:
            modified = transcript.stat().st_mtime
        except OSError:
            continue
        ref = describe(transcript)
        if ref is None or not ref.name:
            continue
        # The directory comes from the transcript's own `cwd`, never from the
        # name of the folder transcripts are filed under: that name replaced
        # every separator with a dash, so `halyard-fleet` and `halyard/fleet`
        # encode identically and decoding produces a path that does not exist.
        project = ref.cwd or "(directory not recorded)"
        # One named conversation spans many session ids; keep the most recent
        # sighting of each name rather than listing it once per restart.
        if ref.name not in seen or modified > seen[ref.name][0]:
            seen[ref.name] = (modified, project, ref.named_by_a_person)

    if not seen:
        print("No named sessions found.")
        return 1

    print("Session names visible on this machine, newest first:\n")
    generated = False
    for name, (modified, project, chosen) in sorted(seen.items(), key=lambda i: -i[1][0]):
        when = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
        generated = generated or not chosen
        print(f"  {when}  {name}{'' if chosen else '   ⚠ auto-titled'}")
        print(f"{'':20}{project}")
    print(
        "\nPut the two you want routed into .env, exactly as printed:\n"
        "  HALYARD_NAVIGATOR_SESSION=...\n"
        "  HALYARD_DRIVER_SESSION=..."
    )
    if generated:
        # Worth interrupting for. A generated title routes correctly the day it
        # is copied and stops without an error the moment Claude rewrites it,
        # which looks like Halyard losing messages rather than like a name
        # having moved underneath it.
        print(
            "\n⚠ Names marked auto-titled were written by Claude, not by you, and\n"
            "  are rewritten as the conversation moves. A seat pointed at one\n"
            "  works until it changes, then quietly routes nothing. Rename the\n"
            "  session in the app first, then copy the name you chose."
        )
    return 0
