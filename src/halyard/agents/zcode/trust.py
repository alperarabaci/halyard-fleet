"""Whether ZCode will run the hooks written into a project.

ZCode holds a workspace's hooks until somebody trusts them, and runs none of
them in the meantime. Its own guide says the opposite — that workspace hooks
have no trust gate — and measured on 3.11.2 that is wrong: the log said
"Project hooks pending workspace trust", every hook stayed `pending_trust`, and
not one fired until trust was given.

Trust is per declaration, by a SHA-256 of it, so rewriting a hook — a new bridge
path, a new timeout — puts it back in the queue. The script a hook runs is not
part of that: a probe's script was edited under a trusted hook and stayed
trusted. So updating this checkout does not take the gate down; moving it does.

Unlike Codex, ZCode answers the question itself. `hooks trust status --json`
lists each hook with its state, and it is a command in the application's own
engine, started the way the application starts it: `ELECTRON_RUN_AS_NODE=1`
and the engine's script. Halyard never grants trust — the review exists so that
a person agrees to what runs from outside the project — but it prints the
command that grants these hooks and nobody else's.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

#: This checkout's bridge scripts: how Halyard's hooks are told apart from
#: anybody else's in ZCode's answer.
BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "bridge"

#: Where macOS keeps the application — for everybody, or for one user who
#: installed it without an administrator.
APPS = (Path("/Applications/ZCode.app"), Path.home() / "Applications" / "ZCode.app")
EXECUTABLE = Path("Contents/MacOS/ZCode")
ENGINE = Path("Contents/Resources/glm/zcode.cjs")

#: The engine answers in about a second; this bounds a machine where it does not.
TIMEOUT_SECONDS = 30

TRUSTED = "trusted_persistent"


def app() -> Path | None:
    """The installed application, with its engine where 3.11.2 keeps it."""
    for candidate in APPS:
        if (candidate / EXECUTABLE).is_file() and (candidate / ENGINE).is_file():
            return candidate
    return None


def _engine(found: Path) -> list[str]:
    return [str(found / EXECUTABLE), str(found / ENGINE)]


def status(project: Path) -> dict | None:
    """ZCode's own account of this workspace's hooks, or None if it gave none."""
    found = app()
    if found is None:
        return None
    try:
        done = subprocess.run(
            [*_engine(found), "hooks", "trust", "status", "--workspace", str(project), "--json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def command(project: Path, action: str, *digests: str) -> str:
    """A `hooks trust` command, as it can be pasted into a terminal."""
    found = app() or APPS[0]
    words = ["ELECTRON_RUN_AS_NODE=1", *(shlex.quote(word) for word in _engine(found))]
    words += ["hooks", "trust", action, "--workspace", shlex.quote(str(project))]
    for digest in digests:
        words += ["--hook-digest", digest]
    return " ".join(words)


def _read(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_the_gate(handler: object) -> bool:
    """A handler running this checkout's `hook.sh`."""
    if not isinstance(handler, dict):
        return False
    arguments = handler.get("args") if isinstance(handler.get("args"), list) else []
    words = [handler.get("command"), *arguments]
    return any(
        isinstance(word, str) and word.startswith(str(BRIDGE_DIR)) and word.endswith("hook.sh")
        for word in words
    )


def _ours(item: object) -> bool:
    return isinstance(item, dict) and str(BRIDGE_DIR) in str(item.get("displayCommand") or "")


def check_wired(hooks_file: Path, project_dir: Path) -> list[tuple[str, str]]:
    """Everything that can leave a wired ZCode project with no gate.

    Three, all of them silent: the file not switching hooks on, the gate missing
    from it, and ZCode not trusting what is there. Returned as `(level, text)`
    so that `wire` and `doctor` say the same thing.
    """
    hooks = _read(hooks_file).get("hooks")
    if not isinstance(hooks, dict) or hooks.get("enabled") is not True:
        return [
            ("fail", f"{hooks_file.name} does not switch hooks on, so ZCode runs none of them"),
            ("", f"put the gate back with: halyard wire {project_dir}"),
        ]
    events = hooks.get("events") if isinstance(hooks.get("events"), dict) else {}
    groups = events.get("PreToolUse") if isinstance(events.get("PreToolUse"), list) else []
    if not any(
        _is_the_gate(handler)
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks") or []
    ):
        return [
            ("fail", "no Halyard gate among its PreToolUse hooks, so nothing is asked for"),
            ("", f"put it back with: halyard wire {project_dir}"),
        ]

    answer = status(project_dir)
    if answer is None:
        return [
            ("warn", "could not ask ZCode whether it trusts these hooks"),
            ("", "it runs none of them until it does. Ask it with:"),
            ("", f"    {command(project_dir, 'status')}"),
        ]
    ours = [item for item in answer.get("items") or [] if _ours(item)]
    if not ours:
        return [("warn", f"ZCode lists none of Halyard's hooks for {project_dir}")]
    waiting = [item for item in ours if item.get("trustState") != TRUSTED]
    if not waiting:
        return [("ok", "ZCode trusts Halyard's hooks here")]
    states = ", ".join(sorted({str(item.get("trustState")) for item in waiting}))
    digests = [
        str(item["hookDeclarationDigest"]) for item in waiting if item.get("hookDeclarationDigest")
    ]
    return [
        ("fail", f"ZCode has not trusted Halyard's hooks here ({states}), so it runs none"),
        ("", "Review them in ZCode, or trust exactly these and no others with:"),
        ("", f"    {command(project_dir, 'grant', *digests)}"),
    ]
