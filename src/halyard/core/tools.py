"""Tools that may run without being asked.

The sibling of `writes.py`, and it exists for the same failure in a different
place. A turn started from a phone runs headless, and Claude Code cannot open a
permission dialog there — so an MCP call or a `WebFetch` that is not on its own
allow list is denied outright, with no card and nothing to answer. At the desk
the same call is a popup; away from it, silence.

Putting those tools behind the gate fixes the silence and creates a second
problem, which this module is the answer to. An MCP server is mostly read-only
queries — `list_companies`, `get_company_snapshot` — and one analysis turn calls
them dozens of times. A card for each would make the phone useless, so a person
names the ones that need not ask:

    tools:
      - mcp__*__list_*
      - mcp__*__get_*

Patterns are glob-matched against the tool's name. `mcp__*__list_*` covers the
same tool on a local server and a production one, which is how these are
actually deployed — measured: `mcp__claude_ai_alpha_explore_prod__list_companies`.

**What it will not grant.** `Bash` is refused, and so is any pattern wide enough
to reach it. A shell command is the thing the gate was built for, and one entry
here would hand it over wholesale; the file tools are refused for the same
reason, because `writes.py` grants those by *destination*, which is the narrower
and more honest question to ask about a write. Everything granted here is
written to the audit log with the pattern that allowed it.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from halyard.core.writes import FILE_TOOLS

#: Never grantable from here, however the pattern is spelled. `Bash` is the
#: gate's whole reason for existing; the file tools have `writes:`, which asks
#: about the destination rather than handing over the tool.
NEVER = frozenset({"Bash"}) | FILE_TOOLS


def allowed_by(tool: str | None, patterns: tuple[str, ...]) -> str | None:
    """The pattern that lets this tool run without asking, or None to ask.

    None is the answer to everything uncertain, because the caller turns None
    into a card on somebody's phone — being wrong here costs a question, never
    a silent grant.
    """
    if not tool or not patterns or tool in NEVER:
        return None
    for pattern in patterns:
        cleaned = pattern.strip()
        if cleaned and fnmatchcase(tool, cleaned):
            return pattern
    return None


def from_yaml(text: str) -> tuple[str, ...]:
    """The `tools:` block: tool names that may run without asking.

    Refuses a shape it cannot honour rather than ignoring it, and refuses a
    pattern that reaches `Bash` or a file tool however it is written — a
    `tools: ["*"]` would otherwise quietly undo the gate.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None

    block: Any = loaded.get("tools") if isinstance(loaded, dict) else None
    if block is None:
        return ()
    if not isinstance(block, list):
        raise ValueError("`tools:` must be a list of tool-name patterns, one per line.")

    patterns: list[str] = []
    for entry in block:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("Every entry under `tools:` must be a tool-name pattern, not empty.")
        cleaned = entry.strip()
        reached = sorted(name for name in NEVER if fnmatchcase(name, cleaned))
        if reached:
            raise ValueError(
                f"`tools:` entry {entry!r} would also grant {', '.join(reached)}, which this "
                "block cannot do. A shell command is what the gate is for, and a write is "
                "granted by its destination under `writes:`."
            )
        patterns.append(cleaned)
    return tuple(patterns)


def load(directory: Path | None = None) -> tuple[str, ...]:
    """Patterns from the configuration, or none at all if there is no file."""
    from halyard.core.config_file import find_config

    path = find_config(directory)
    if path is None:
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    try:
        return from_yaml(text)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
