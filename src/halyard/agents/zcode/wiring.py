"""Putting the gate on a ZCode project, and taking it off again.

ZCode reads a workspace's hooks from `.zcode/config.json`, under
`hooks.events`, and runs none of them unless `hooks.enabled` is true. Both are
measured, and the second is silent: a file with the events and without the
switch is a project with no gate that looks gated.

That file is the workspace's whole configuration — its MCP servers, its
plugins — and a team may keep it in version control, so this merges into it,
keeps a copy first, and names everything it left alone.

**Each hook says which runtime it belongs to.** Nothing in a call does: the
payload is Claude Code's with camelCase copies of every field beside it, which
on their own read as Antigravity's, and its transcript is a temporary file in no
runtime's home, which reads as Claude Code's. So the command sets
`HALYARD_RUNTIME=zcode` for the bridge to find. It runs through `/usr/bin/env`
as a process rather than through a shell, because an argument vector needs no
quoting for a checkout whose path has a space in it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

CONFIG = Path(".zcode") / "config.json"

#: Everything the gate covers, by the names ZCode gives them — Claude Code's,
#: measured for `Bash` and `Write`. Not `AskUserQuestion`: answering one from a
#: phone is written for Claude Code alone. ZCode's matcher is a regular
#: expression, and it folds `ApplyPatch` into `Write` and `Edit`.
MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__.*"

#: How long each may run, in milliseconds. ZCode's default is a minute, and an
#: approval can wait longer than that: past it the hook is killed, and the answer
#: from the phone arrives for nobody.
GATE_TIMEOUT_MS = 600_000
RELAY_TIMEOUT_MS = 15_000

#: What each hook says it is. See the module docstring.
DECLARATION = "HALYARD_RUNTIME=zcode"

#: The gate and the relay, by bridge script.
SCRIPTS = {"PreToolUse": ("hook.sh", GATE_TIMEOUT_MS), "Stop": ("relay.py", RELAY_TIMEOUT_MS)}


def groups(bridges: Path) -> dict[str, dict]:
    """The group each event gets, written exactly this way every time."""
    wanted = {}
    for event, (script, timeout) in SCRIPTS.items():
        handler = {
            "type": "process",
            "command": "/usr/bin/env",
            "args": [DECLARATION, str(bridges / script)],
            "timeoutMs": timeout,
        }
        matcher = {"matcher": MATCHER} if event == "PreToolUse" else {}
        wanted[event] = {**matcher, "hooks": [handler]}
    return wanted


def _words(handler: object) -> list[str]:
    if not isinstance(handler, dict):
        return []
    arguments = handler.get("args") if isinstance(handler.get("args"), list) else []
    return [word for word in (handler.get("command"), *arguments) if isinstance(word, str)]


def _ours(handler: object, bridges: Path) -> bool:
    """A handler running one of this install's bridge scripts."""
    return any(str(bridges) in word for word in _words(handler))


def _dead(handler: object) -> bool:
    """One of Halyard's, from a checkout that is not on this machine.

    A committed `.zcode/config.json` travels between machines with each one's
    absolute path in it. The one that cannot run here is dropped, rather than
    left beside this machine's own.
    """
    return any(
        word.endswith(("/bridge/hook.sh", "/bridge/relay.py")) and not Path(word).exists()
        for word in _words(handler)
    )


def _read(path: Path) -> dict:
    """The workspace's configuration, or an empty one. Raises if unreadable."""
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("it does not hold a JSON object")
    return loaded


def _back_up(path: Path) -> Path | None:
    """A timestamped copy, so that two runs never overwrite the one good version."""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _left_alone(document: dict) -> list[tuple[str, str]]:
    kept = sorted(key for key in document if key != "hooks")
    return [("", f"left untouched in that file: {', '.join(kept)}")] if kept else []


def install(project: Path, bridges: Path) -> list[tuple[str, str]]:
    """Write the gate and the relay, and switch hooks on."""
    for script, _ in SCRIPTS.values():
        if not (bridges / script).is_file():
            return [("fail", f"{bridges / script} is missing from this install")]

    path = project / CONFIG
    try:
        document = _read(path)
    except (OSError, ValueError) as unreadable:
        return [("fail", f"{CONFIG} could not be read ({unreadable}), so nothing was written")]
    hooks = document.get("hooks", {})
    events = hooks.get("events", {}) if isinstance(hooks, dict) else None
    if not isinstance(events, dict):
        return [("fail", f"{CONFIG} has a `hooks` block this does not recognise; left alone")]

    events = dict(events)
    written = []
    for event, group in groups(bridges).items():
        before = events.get(event) or []
        if not isinstance(before, list):
            return [("fail", f"{CONFIG}: `hooks.events.{event}` is not a list; left alone")]
        kept = []
        for existing in before:
            handlers = existing.get("hooks") if isinstance(existing, dict) else None
            if not isinstance(handlers, list):
                kept.append(existing)
                continue
            others = [h for h in handlers if not (_ours(h, bridges) or _dead(h))]
            if others:
                kept.append({**existing, "hooks": others})
        after = [*kept, group]
        if after != before:
            events[event] = after
            written.append(event)

    switched_off = hooks.get("enabled") is False
    if not written and hooks.get("enabled") is True:
        return [("", f"{CONFIG} already carries the current gate")]

    document["hooks"] = {**hooks, "enabled": True, "events": events}
    backup = _back_up(path)
    try:
        _write(path, document)
    except OSError as unwritable:
        return [("fail", f"could not write {path}: {unwritable}")]

    said: list[tuple[str, str]] = []
    if written:
        said.append(("ok", f"wrote {', '.join(written)} into {CONFIG}"))
    if switched_off:
        said.append(("ok", '"enabled": false was turned on — it switched off every hook there'))
    elif not written:
        said.append(("ok", f"switched hooks on in {CONFIG}; ZCode runs none of them without it"))
    if backup:
        said.append(("", f"previous version kept at {backup}"))
    return said + _left_alone(document)


def uninstall(project: Path, bridges: Path) -> list[tuple[str, str]]:
    """Take out what this install wrote, and nothing else."""
    path = project / CONFIG
    if not path.is_file():
        return []
    try:
        document = _read(path)
    except (OSError, ValueError) as unreadable:
        return [("fail", f"{CONFIG} could not be read ({unreadable}), so nothing was removed")]
    hooks = document.get("hooks")
    events = hooks.get("events") if isinstance(hooks, dict) else None
    if not isinstance(events, dict):
        return []

    removed: set[str] = set()
    remaining = {}
    for event, before in events.items():
        if not isinstance(before, list):
            remaining[event] = before
            continue
        kept = []
        for existing in before:
            handlers = existing.get("hooks") if isinstance(existing, dict) else None
            if not isinstance(handlers, list):
                kept.append(existing)
                continue
            others = [h for h in handlers if not _ours(h, bridges)]
            if len(others) != len(handlers):
                removed.add(event)
            if others:
                kept.append({**existing, "hooks": others})
        if kept:
            remaining[event] = kept
    if not removed:
        return []

    if remaining:
        document["hooks"] = {**hooks, "events": remaining}
    else:
        # Nothing left to run, so the switch and its settings go with it rather
        # than staying behind as a husk somebody has to work out the meaning of.
        document.pop("hooks")
    backup = _back_up(path)
    try:
        _write(path, document)
    except OSError as unwritable:
        return [("fail", f"could not write {path}: {unwritable}")]

    said = [("ok", f"removed {', '.join(sorted(removed))} from {CONFIG}")]
    if backup:
        said.append(("", f"previous version kept at {backup}"))
    return said + _left_alone(document)
