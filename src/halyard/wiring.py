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

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

#: `PreToolUse` is the gate. `Stop` is the relay that sends replies to a phone.
WIRING = (
    ("PreToolUse", "Bash", BRIDGE_DIR / "hook.sh", 600),
    ("Stop", None, BRIDGE_DIR / "relay.py", 15),
)

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


def settings_path(directory: Path) -> Path:
    return project_root(directory) / ".claude" / "settings.local.json"


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
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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


def wire(directory: Path) -> int:
    """Add the hooks, keeping everything already in the file."""
    path = settings_path(directory)
    config = _load(path)
    hooks = config.setdefault("hooks", {})

    added = []
    for event, matcher, script, timeout in WIRING:
        if not script.exists():
            print(f"halyard: {script} is missing from this install")
            return 1
        groups = hooks.setdefault(event, [])
        if any(
            _is_ours(hook.get("command"))
            for group in groups
            for hook in (group or {}).get("hooks") or []
        ):
            continue
        entry: dict = {"hooks": [{"type": "command", "command": str(script), "timeout": timeout}]}
        if matcher:
            entry["matcher"] = matcher
        groups.append(entry)
        added.append(event)

    if not added:
        print(f"Already wired: {path}")
    else:
        backup = _back_up(path)
        _write(path, config)
        print(f"Wired {', '.join(added)} into {path}")
        if backup:
            print(f"Previous version kept at {backup}")
        kept = sorted(k for k in config if k != "hooks")
        if kept:
            # Say it out loud. Losing this silently is the failure this whole
            # module exists to prevent, and "nothing was reported" is not the
            # same reassurance as "your permissions are still there".
            print(f"Left untouched in that file: {', '.join(kept)}")

    print(f"\nRestart the session — hooks are read at startup.\n\n{RULES}")
    return 0


def unwire(directory: Path) -> int:
    """Remove only this install's hooks, and nothing else."""
    path = settings_path(directory)
    if not path.exists():
        print(f"Nothing to remove: {path} does not exist")
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
        print(f"Nothing of this Halyard install is wired into {path}")
        return 0

    backup = _back_up(path)
    _write(path, config)
    print(f"Removed {', '.join(sorted(set(removed)))} from {path}")
    if backup:
        print(f"Previous version kept at {backup}")
    kept = sorted(k for k in config if k != "hooks")
    if kept:
        print(f"Left untouched in that file: {', '.join(kept)}")
    print("\nRestart the session — hooks are read at startup.")
    print("Bash no longer goes through Halyard in this project.")
    return 0
