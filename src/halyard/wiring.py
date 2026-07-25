"""Adding and removing the gate, without destroying anything on the way.

`.claude/settings.local.json` is not Halyard's file. Claude Code writes to it
too — every "don't ask again" appends a rule to a `permissions.allow` list that
lives there — and the file is gitignored, so nothing keeps a copy but you.

The README used to say "put this JSON in that file", showing a document with
only `hooks` in it. Followed literally, that deletes the permission list. It
happened, on 2026-07-22, and the symptom was not an error: approvals kept
working and a session simply started asking again about commands it had settled
months earlier. Nobody connects that to a config edit from days before.

So wiring is a merge, unwiring removes only what this install put there, and
both take a copy first.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from halyard.agents import registry
from halyard.agents.spec import RuntimeSpec

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

#: `PreToolUse` is the gate. `Stop` is the relay that sends replies to a phone.
#: Every runtime gets both; anything else it needs is in its own `hooks.extra`.
#:
#: Scripts by name, matching the spec, so a runtime package never has to know
#: where this checkout keeps its bridge.
WIRING = (
    ("PreToolUse", "Bash", "hook.sh", 600),
    ("Stop", None, "relay.py", 15),
)

#: The top-level key a `named`-dialect file gets its hooks under.
#:
#: Antigravity's document is not `{"hooks": {...}}` like the other two: each
#: top-level key is a *hook name* whose value holds that hook's events, and
#: every name contributing to an event is merged and run in turn. So it is a
#: namespace rather than a wrapper, and two tools can gate one project without
#: either having to know about the other. This is what Halyard calls itself
#: there — a wiring concern, not something a runtime package should have an
#: opinion about.
HOOK_NAME = "halyard"


RULES = """\
Three things to know before you walk away from this machine:

  1. This project now needs the control plane running. While the hook is
     wired, a Bash command with Halyard down is DENIED — every one of them,
     including `ls`. `halyard unwire` puts the project back.

  2. It is live as soon as it starts. Approvals go to Telegram from the
     first command; there is no arming step. `/pause` is what stops it, and
     pausing needs the server running too.

  3. An approval expires. Nobody answers within the approval timeout and it
     is denied, not left waiting.
"""


def project_root(directory: Path) -> Path:
    """Where Claude Code looks for `.claude/`: the repository root.

    Measured — a session opened in a subdirectory picks up hooks from the top
    of its repository, and picks up nothing at all when there is no repository
    above it.
    """
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


def settings_path(directory: Path, runtime: RuntimeSpec | None = None) -> Path:
    spec = runtime or next(iter(registry.discover().values()))
    return spec.settings_path(project_root(directory))


def installed(runtimes: tuple[RuntimeSpec, ...] | None = None) -> tuple[RuntimeSpec, ...]:
    """The runtimes whose CLI is on this machine.

    Wiring is offered for these and removal is attempted for all of them. The
    asymmetry is deliberate: adding a gate for a runtime nobody has is clutter,
    while leaving one behind after a CLI is uninstalled is a hook pointing at a
    bridge nothing will ever call.
    """
    candidates = runtimes if runtimes is not None else tuple(registry.discover().values())
    return tuple(r for r in candidates if r.on_this_machine())


def targets(given: str | None) -> list[Path]:
    """Which projects a wire or unwire is about.

    **With nothing given, the configuration is the answer — not `cwd`.** Where
    you are standing when you run `halyard wire` is almost always the Halyard
    checkout, so defaulting to the working directory gated Halyard with its own
    bridge: the control plane's every command then went through the hook it was
    serving. Nobody asks for that, and nothing about it is obvious afterwards.

    An explicit argument still wins, as a project name or a directory. Only a
    machine with no configuration at all falls back to where you are, because
    then there is nothing else to go on.
    """
    from halyard.core.config_file import projects, resolve_project

    if given is not None:
        candidate = Path(given).expanduser()
        return [candidate if candidate.is_dir() else resolve_project(given)]

    described = [project.path.expanduser() for project in projects() if project.path]
    return described or [Path.cwd()]


def configured_for(directory: Path) -> tuple[RuntimeSpec, ...] | None:
    """The runtimes this project's seats are written for, or None if it has none.

    **The configuration decides what gets wired, not the machine.** Asking what
    is installed answers a different question and answers it wrongly in both
    directions: a project whose seats are all Claude Code had Antigravity's
    hooks written into it because Antigravity happened to be on that Mac, and
    Claude Code's own hooks were skipped because its binary lives inside an app
    bundle and was not on `PATH`. The file said exactly which runtimes that
    project uses, and nothing read it.

    `None` means the configuration does not describe this directory at all —
    somebody wiring a project before writing seats for it — and only then is
    "what is installed" the best guess available.
    """
    from halyard.core.config_file import projects

    try:
        described = projects()
    except ValueError:
        # A configuration that cannot be read is not a configuration that says
        # nothing. Refusing to guess is the whole point of this function.
        return None
    if not described:
        return None

    root = project_root(directory).resolve()
    for project in described:
        if project.path is None or project.path.expanduser().resolve() != root:
            continue
        wanted = {seat.runtime for seat in project.seats}
        found = registry.discover()
        return tuple(found[name] for name in found if name in wanted)
    return None


def _fallback() -> tuple[RuntimeSpec, ...]:
    """What to wire when nothing looks installed.

    A PATH that hides the CLI is a likelier explanation than a machine with no
    agent on it at all, so one runtime is wired rather than none — and it is
    the first the registry found, which is stable across machines.
    """
    found = tuple(registry.discover().values())
    return found[:1]


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"halyard: {path} is not valid JSON ({error}). Not touching it.") from None
    if not isinstance(loaded, dict):
        raise SystemExit(f"halyard: {path} does not contain a JSON object. Not touching it.")
    return loaded


def _back_up(path: Path) -> Path | None:
    """Copy the file aside before writing it.

    Timestamped rather than a single `.bak`, because the mistake worth
    protecting against is running this twice — a fixed name would overwrite the
    good copy with the already-damaged one on the second run.
    """
    if not path.exists():
        return None
    # Microseconds keep two material rewrites in the same second from reusing
    # one path. A backup operation must never overwrite an earlier backup:
    # that earlier copy may be the only version containing a lost allowlist.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def _write(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _is_ours(command: object) -> bool:
    """Whether this hook entry is one this install put there.

    Path-based, so unwiring cannot remove somebody else's hook — including the
    same script from a second Halyard checkout, which is a real shape: two
    machines sharing a settings file through a synced directory.
    """
    return isinstance(command, str) and str(BRIDGE_DIR.resolve()) in command


def named_handlers(document: dict, event: str, grouped: tuple[str, ...]) -> list[dict]:
    """Every handler one named hook has for one event, whichever shape it is in.

    Flattens the two so callers can ask one question. Reading a grouped event
    as flat yields the group itself — an object with a `matcher` and no
    `command` — which reads as a hook pointing nowhere.

    `grouped` is passed rather than looked up: this is the shape routine, and a
    module that decides for itself which events a runtime groups is the module
    that has to be edited when a fourth runtime groups different ones.
    """
    entries = document.get(event) or []
    if not isinstance(entries, list):
        return []
    if event in grouped:
        return [h for group in entries for h in (group or {}).get("hooks") or []]
    return [entry for entry in entries if isinstance(entry, dict)]


def _wire_antigravity(directory: Path, runtime: RuntimeSpec) -> int:
    """Add the gate to `.agents/hooks.json`, in Antigravity's own shape."""
    path = settings_path(directory, runtime)
    config = _load(path)
    document = config.setdefault(HOOK_NAME, {})
    if not isinstance(document, dict):
        raise SystemExit(f"halyard: {path} has a {HOOK_NAME!r} that is not an object.")

    added = []
    for event, _matcher, script_name, timeout in WIRING + runtime.hooks.extra:
        script = BRIDGE_DIR / script_name
        if not script.exists():
            print(f"halyard: {script} is missing from this install")
            return 1
        already = named_handlers(document, event, runtime.hooks.grouped)
        if any(_is_ours(h.get("command")) for h in already):
            continue
        handler = {"type": "command", "command": str(script), "timeout": timeout}
        entries = document.setdefault(event, [])
        entries.append(
            {"matcher": runtime.hooks.matcher, "hooks": [handler]}
            if event in runtime.hooks.grouped
            else handler
        )
        added.append(event)

    # `enabled: false` turns off every handler under this name while leaving
    # the file looking exactly like a wired one. Wiring means the gate works,
    # so it is turned back on — and said out loud, because somebody put it
    # there on purpose and deserves to know it was undone.
    re_enabled = document.get("enabled") is False
    if re_enabled:
        document["enabled"] = True

    if not added and not re_enabled:
        print(f"  {runtime.name}: already wired ({path})")
        return 0

    backup = _back_up(path)
    _write(path, config)
    if added:
        print(f"  {runtime.name}: wired {', '.join(added)} into {path}")
    if re_enabled:
        print(f'  {runtime.name}: "enabled": false was turned back on — it disabled the gate')
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != HOOK_NAME)
    if kept:
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 0


def _unwire_antigravity(directory: Path, runtime: RuntimeSpec) -> int:
    path = settings_path(directory, runtime)
    if not path.exists():
        return 0
    config = _load(path)
    document = config.get(HOOK_NAME)
    if not isinstance(document, dict):
        return 0

    removed = []
    for event in [key for key in document if key != "enabled"]:
        entries = document.get(event) or []
        if event in runtime.hooks.grouped:
            kept = []
            for group in entries:
                before = (group or {}).get("hooks") or []
                mine = [h for h in before if not _is_ours(h.get("command"))]
                if len(mine) != len(before):
                    removed.append(event)
                if mine:
                    kept.append({**group, "hooks": mine})
        else:
            kept = [h for h in entries if not _is_ours((h or {}).get("command"))]
            if len(kept) != len(entries):
                removed.append(event)
        if kept:
            document[event] = kept
        else:
            del document[event]

    if not removed:
        return 0
    # A name holding nothing but `enabled` is not a hook, so the whole key goes
    # rather than being left as a husk somebody has to work out the meaning of.
    if not [key for key in document if key != "enabled"]:
        config.pop(HOOK_NAME, None)

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: removed {', '.join(sorted(set(removed)))} from {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept_keys = sorted(k for k in config if k != HOOK_NAME)
    if kept_keys:
        print(f"    left untouched in that file: {', '.join(kept_keys)}")
    return 1


def _wire_one(directory: Path, runtime: RuntimeSpec) -> int:
    """Add one runtime's hooks, keeping everything already in its file."""
    if runtime.hooks.dialect == "named":
        return _wire_antigravity(directory, runtime)
    path = settings_path(directory, runtime)
    config = _load(path)
    hooks = config.setdefault("hooks", {})

    added = []
    runtime_wiring = WIRING + runtime.hooks.extra
    for event, matcher, script_name, timeout in runtime_wiring:
        script = BRIDGE_DIR / script_name
        if not script.exists():
            print(f"halyard: {script} is missing from this install")
            return 1
        groups = hooks.setdefault(event, [])
        mine = [
            group
            for group in groups
            for hook in (group or {}).get("hooks") or []
            if _is_ours(hook.get("command"))
        ]
        if mine:
            # Already wired, but possibly with a matcher from an older release.
            # Leaving a stale one in place is the quiet kind of wrong: the file
            # is present, `doctor` is happy, and half the tool calls are not
            # gated. Correcting it costs a re-review of the hook, which is the
            # honest price of the matcher having been incomplete.
            wanted = runtime.hooks.matcher if matcher == "Bash" else matcher
            for group in mine:
                if wanted and group.get("matcher") != wanted:
                    group["matcher"] = wanted
                    added.append(f"{event} (matcher corrected)")
            continue
        # An absolute path, always. Claude Code expands `$CLAUDE_PROJECT_DIR`
        # and Codex expands nothing of the kind — it has no project variable at
        # all, only `$CODEX_HOME`. A hooks file written with the Claude
        # variable in it does not fail to load under Codex; the hook runs and
        # dies looking for a directory called `$CLAUDE_PROJECT_DIR`, which is
        # what "hook: Stop Failed" meant when this repository's own file had it.
        entry: dict = {"hooks": [{"type": "command", "command": str(script), "timeout": timeout}]}
        if matcher:
            entry["matcher"] = runtime.hooks.matcher if matcher == "Bash" else matcher
        groups.append(entry)
        added.append(event)

    if not added:
        print(f"  {runtime.name}: already wired ({path})")
        return 0

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: wired {', '.join(added)} into {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != "hooks")
    if kept:
        # Say it out loud. Losing this silently is the failure this whole
        # module exists to prevent, and "nothing was reported" is not the
        # same reassurance as "your permissions are still there".
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 0


def wire(directory: Path, runtimes: tuple[RuntimeSpec, ...] | None = None) -> int:
    """Put the gate on a project, for the runtimes its seats are written for.

    **The configuration decides.** It says which runtimes work in this project,
    which is the actual question — and asking the machine instead got it wrong
    both ways at once: a project configured with two Claude Code seats had
    Antigravity's hooks written into it, because Antigravity was installed, and
    Claude Code's own were skipped, because its binary lives inside an app
    bundle rather than on `PATH`.

    Only a project the configuration does not describe falls back to what is
    installed — there is nothing better to go on, and wiring nothing at all
    would leave somebody with no gate and no explanation.
    """
    described = configured_for(directory) if runtimes is None else None
    chosen = runtimes if runtimes is not None else described
    if chosen is None:
        chosen = registry.installed() or _fallback()
    print(f"Wiring {project_root(directory)}")
    if described is not None and not described:
        # Described and empty is a real answer: the project exists in the
        # configuration with no seats. Saying so beats wiring by guesswork.
        print("  no seats are configured for this project, so nothing to wire")
        return 0
    for runtime in chosen:
        if _wire_one(directory, runtime):
            return 1
        if not runtime.on_this_machine():
            # Configured but not here. The hooks are written anyway — this is
            # a shared file and the machine that runs that runtime may be
            # another one — but silence would read as "all set".
            print(f"    note: no {runtime.human} CLI found on this machine")

    for runtime in chosen:
        if runtime.check_wired is None:
            continue
        root = project_root(directory)
        # Loud, because the failure it prevents is silent: a runtime can be
        # wired correctly and still run none of it.
        for level, text in runtime.check_wired(settings_path(directory, runtime), root):
            if level == "fail":
                print(f"\n⚠ {runtime.human}: {text}")
            elif level == "warn":
                print(f"\n  {runtime.human}: {text}")

    print(f"\nRestart the session — hooks are read at startup.\n\n{RULES}")
    return 0


def _unwire_one(directory: Path, runtime: RuntimeSpec) -> int:
    """Remove only this install's hooks, and nothing else."""
    if runtime.hooks.dialect == "named":
        return _unwire_antigravity(directory, runtime)
    path = settings_path(directory, runtime)
    if not path.exists():
        return 0

    config = _load(path)
    hooks = config.get("hooks") or {}
    removed = []
    for event in list(hooks):
        groups = hooks.get(event) or []
        kept_groups = []
        for group in groups:
            before = (group or {}).get("hooks") or []
            entries = [h for h in before if not _is_ours(h.get("command"))]
            if len(entries) != len(before):
                removed.append(event)
            if entries:
                kept_groups.append({**group, "hooks": entries})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        config.pop("hooks", None)

    if not removed:
        return 0

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: removed {', '.join(sorted(set(removed)))} from {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != "hooks")
    if kept:
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 1


def unwire(directory: Path, runtimes: tuple[RuntimeSpec, ...] | None = None) -> int:
    """Take the gate off, wherever it was put.

    Every runtime by default, not just the installed ones: a hook left behind
    after a CLI was removed still points at a bridge, and the next person to
    install that CLI inherits a gate they never asked for.
    """
    chosen = runtimes if runtimes is not None else tuple(registry.discover().values())
    touched = sum(_unwire_one(directory, runtime) for runtime in chosen)
    if not touched:
        print(f"Nothing of this Halyard install is wired into {project_root(directory)}")
        return 0
    print("\nRestart the session — hooks are read at startup.")
    print("Bash no longer goes through Halyard in this project.")
    return 0
