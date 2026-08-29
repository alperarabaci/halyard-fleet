"""Which applications this machine can be asked to open.

Its own list, deliberately not the runtime registry. The two overlap today and
are not the same set: an application can be worth opening long before anything
can drive it — opencode is the expected case — and a runtime can be a command
with no application at all. Hanging this off `RuntimeSpec` would mean the first
openable non-runtime had to be declared a runtime to get in.

The shipped list lives in `known.yaml` beside this file, and `applications:` in
halyard.yaml adds to it or overrides an entry by name. A bad entry there is
skipped with a warning rather than taking the control plane down: this opens
applications, and nothing about it is worth losing the gate over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: The shipped list, beside this module.
KNOWN = Path(__file__).with_name("known.yaml")


@dataclass(frozen=True)
class Application:
    """One application, by the id that survives it being moved or renamed."""

    name: str
    bundle_id: str
    #: Where it usually installs, for a machine whose Spotlight index is off or
    #: still building. A last resort, not the first question.
    fallback: Path | None = None
    #: Other names somebody might type. The name a person reaches for is often
    #: not the application's own.
    aliases: tuple[str, ...] = field(default=())

    def answers_to(self, typed: str) -> bool:
        wanted = typed.strip().lower()
        return wanted == self.name.lower() or wanted in {a.lower() for a in self.aliases}


def _one(name: str, body: object) -> Application | None:
    """One entry, or None if it does not describe an application."""
    if not isinstance(body, dict):
        logger.warning("Ignoring application %r: it is not a mapping", name)
        return None
    bundle_id = str(body.get("bundle_id") or "").strip()
    if not bundle_id:
        logger.warning("Ignoring application %r: it has no bundle_id", name)
        return None
    raw_aliases = body.get("aliases") or ()
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    fallback = str(body.get("fallback") or "").strip()
    return Application(
        name=name,
        bundle_id=bundle_id,
        fallback=Path(fallback).expanduser() if fallback else None,
        aliases=tuple(str(a).strip() for a in raw_aliases if str(a).strip()),
    )


def from_yaml(text: str) -> list[Application]:
    """Read a mapping of name to application."""
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError("`applications:` must be a mapping of name to its settings.")
    found = [_one(str(name), body) for name, body in loaded.items()]
    return [app for app in found if app is not None]


def _configured(directory: Path | None = None) -> list[Application]:
    """The `applications:` block, or nothing if there is none."""
    from halyard.core.config_file import find_config

    path = find_config(directory)
    if path is None:
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        logger.warning("Could not read applications from %s: %s", path, error)
        return []
    block = loaded.get("applications") if isinstance(loaded, dict) else None
    if block is None:
        return []
    try:
        return from_yaml(yaml.safe_dump(block))
    except ValueError as error:
        logger.warning("Ignoring the `applications:` block: %s", error)
        return []


def known(directory: Path | None = None) -> list[Application]:
    """Everything openable, shipped list first and configuration on top.

    Overriding by name rather than merging field by field. An application that
    moved to a different bundle id is a different application, and a half-merged
    entry — new id, old fallback — would be neither.
    """
    try:
        shipped = from_yaml(KNOWN.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("Could not read the shipped application list: %s", error)
        shipped = []
    by_name = {app.name: app for app in shipped}
    for app in _configured(directory):
        by_name[app.name] = app
    return list(by_name.values())


def resolve(typed: str, directory: Path | None = None) -> Application | None:
    """The application somebody meant, by name or alias."""
    for app in known(directory):
        if app.answers_to(typed):
            return app
    return None
