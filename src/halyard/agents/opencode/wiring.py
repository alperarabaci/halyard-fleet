"""Putting the gate on a project, and taking it off again.

Two halves, and the second one is the half that is easy to forget.

The plugin is a file dropped in `.opencode/plugins/`, which the runtime loads
at startup with no config entry and no package install — measured, in the TUI
as well as under `opencode serve`, because the TUI embeds the same server.

The other half is that **opencode does not ask by default**. Measured on a
real session: fifteen turns, a file edited, and not one permission event. A
project with the plugin and without a `permission` block has a gate that is
present, wired, loaded, and never consulted — the exact failure this project
has already had three times with hooks that were written in the wrong shape.

So the block is written too, and `doctor` reads it back. It is the runtime's
own configuration file, shared with whatever else the project keeps there, so
it is merged rather than replaced and every other key is named out loud
afterwards. That file is committed in this project's own case: it carries the
`instructions` list, which is somebody's work.

What is asked for is `"ask"` on every category opencode gates, rather than a
narrower set of patterns. Which commands are worth a person is Halyard's
question, answered from `halyard.yaml` and written to the audit log with the
pattern that allowed it; a second list of exceptions living in the runtime's
config would be a place for permissions to be granted that nothing audits and
`/pause` does not reach.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the runtime looks for project plugins, and what this one is called
#: once it is there. The name is ours: several plugins can sit in that
#: directory and only this one is Halyard's to add or remove.
PLUGINS = Path(".opencode") / "plugins"
PLUGIN = "halyard.ts"

#: The bridge, by name, in whichever directory this install keeps them.
SOURCE = "opencode.ts"

#: The project's own configuration. `opencode.jsonc` is also read, and is not
#: written here: choosing between two files a project may have is guesswork,
#: and the one without comments is the one that can be rewritten safely.
CONFIG = "opencode.json"

#: What the runtime has to be told to ask about. Every category it gates —
#: measured from its own configuration schema — because deciding which of them
#: deserve a person is not a decision to leave in this file.
#:
#: `doom_loop` is left out: it is the runtime noticing it is going in circles,
#: which is not a permission a person grants.
ASK = {
    "edit": "ask",
    "bash": "ask",
    "webfetch": "ask",
    "external_directory": "ask",
}


def _read(path: Path) -> dict:
    """The project's configuration, or an empty one, never an exception."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", path, unreadable)
        raise
    return loaded if isinstance(loaded, dict) else {}


def install(project: Path, bridges: Path) -> list[tuple[str, str]]:
    """Write the plugin, and tell the runtime to ask."""
    said: list[tuple[str, str]] = []

    source = bridges / SOURCE
    if not source.is_file():
        return [("fail", f"{source} is missing from this install")]

    wanted = project / PLUGINS / PLUGIN
    try:
        wanted.parent.mkdir(parents=True, exist_ok=True)
        already = wanted.read_text(encoding="utf-8") if wanted.is_file() else None
        bridge = source.read_text(encoding="utf-8")
        if already == bridge:
            said.append(("", f"{wanted.relative_to(project)} is already the current one"))
        else:
            wanted.write_text(bridge, encoding="utf-8")
            said.append(("ok", f"wrote {wanted.relative_to(project)}"))
    except OSError as unwritable:
        return [("fail", f"could not write {wanted}: {unwritable}")]

    # Copied rather than symlinked. A link into this checkout breaks the moment
    # Halyard is moved or the project is opened on another machine, and it
    # breaks by loading nothing — which is the silent kind.
    config = project / CONFIG
    try:
        document = _read(config)
    except (OSError, ValueError):
        return [
            ("fail", f"{CONFIG} could not be read, so the gate was not switched on"),
            ("", "fix that file and run this again; the plugin is in place already"),
        ]

    permission = document.get("permission")
    permission = dict(permission) if isinstance(permission, dict) else {}
    missing = {name: how for name, how in ASK.items() if permission.get(name) != how}
    if not missing:
        said.append(("", f"{CONFIG} already asks about {', '.join(sorted(ASK))}"))
        return said

    kept = sorted(k for k in document if k != "permission")
    document["permission"] = {**permission, **missing}
    try:
        config.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8")
    except OSError as unwritable:
        return [
            *said,
            ("fail", f"could not write {config}: {unwritable}"),
            ("", "without a `permission` block the runtime never asks, and the gate is dead"),
        ]

    said.append(("ok", f"set {', '.join(sorted(missing))} to ask in {CONFIG}"))
    if kept:
        # Said out loud, the same way the hooks path says it. This is somebody
        # else's file — in this project it carries the `instructions` list —
        # and "nothing was reported" is not the same as "it is still there".
        said.append(("", f"left untouched in that file: {', '.join(kept)}"))
    return said


def uninstall(project: Path, bridges: Path) -> list[tuple[str, str]]:
    """Remove the plugin, and put the asking back the way it was found.

    Only what this wrote. The `permission` block is left alone: switching it
    back off would be Halyard deciding a project should stop asking, which is
    not its call to make and not what taking the gate off means.
    """
    said: list[tuple[str, str]] = []
    wanted = project / PLUGINS / PLUGIN
    if wanted.is_file():
        try:
            wanted.unlink()
            said.append(("ok", f"removed {wanted.relative_to(project)}"))
        except OSError as stuck:
            return [("fail", f"could not remove {wanted}: {stuck}")]

    if not said:
        return []

    if (project / CONFIG).is_file():
        said.append(
            ("", f"the `permission` block in {CONFIG} is left as it is — the runtime goes on")
        )
        said.append(("", "asking, and answers them at the desk"))
    return said
