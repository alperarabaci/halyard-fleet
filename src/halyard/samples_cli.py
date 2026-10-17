"""`halyard samples add` — put a sample's project into this machine's halyard.yaml.

A sample ships the project block it needs (`samples/<name>/halyard.yaml.example`),
and trying one meant copying that block into a file somebody had already
written by hand, with their own settings, their own projects and their own
comments in it. This adds it for them, and keeps everything that was there:

- the file is backed up first, as `halyard init` does — `halyard.yaml.<stamp>.bak`;
- the block is added to the text, not the file rewritten, so comments and every
  other project stay exactly as they were;
- the result is read back with the control plane's own reader before anything is
  written, and must still hold every project it held before;
- a project of the same name is left alone, and nothing is written.

The sample's own `chat:` is a placeholder, so it is given the file's default
group, `TELEGRAM_CHAT_ID`, when there is one.

With no `halyard.yaml` yet, the sample's example is the file — its settings still
placeholders, to fill in or to replace by running `halyard init`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from halyard.core.config_file import projects_from_yaml
from halyard.setup_cli import back_up

#: Where the samples are, in this checkout.
CHECKOUT = Path(__file__).resolve().parent.parent.parent
SAMPLES = CHECKOUT / "samples"

USAGE = """usage: halyard samples add <sample> <path>

  Adds the sample's project to halyard.yaml, with <path> as where it is.
  <sample> is a folder under samples/, or its number: 01-one-agent, or 01.
  Copy the sample to <path> first; its README says how.
"""

Say = Callable[[str], None]


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "add":
        print(USAGE, file=sys.stderr)
        return 2
    return add(argv[1], Path(argv[2]), config=Path("halyard.yaml"))


def add(
    sample: str,
    where: Path,
    *,
    config: Path,
    now: str | None = None,
    say: Say = print,
) -> int:
    """Add `sample`'s project to `config`, found at `where`. 0 when it was added."""
    folder = _sample(sample)
    if folder is None:
        known = ", ".join(path.name for path in sorted(SAMPLES.glob("[0-9][0-9]-*")))
        say(f"No sample called {sample!r}. There are: {known or 'none'}.")
        return 2
    where = where.expanduser().resolve()
    if not where.is_dir():
        say(f"{where} is not there yet. Copy the sample to it first; its README says how.")
        return 2
    if where == CHECKOUT or CHECKOUT in where.parents:
        say(
            f"{where} is inside this Halyard checkout, and `halyard wire` gates the "
            "repository a folder is in — that would be Halyard's own. Copy the sample out."
        )
        return 2

    example = (folder / "halyard.yaml.example").read_text(encoding="utf-8")
    [(name, block)] = (yaml.safe_load(example).get("projects") or {}).items()
    block = {**block, "path": str(where)}

    if config.exists():
        text = config.read_text(encoding="utf-8")
        written = yaml.safe_load(text) or {}
        before = set((written.get("projects") or {}) if isinstance(written, dict) else {})
        if name in before:
            say(f"{name} is already in {config}; nothing was changed.")
            return 1
        chat = str(((written.get("settings") or {}).get("TELEGRAM_CHAT_ID")) or "")
        if chat:
            block = _with_chat(block, chat)
        try:
            text = _with_project(text, name, block)
        except ValueError as refused:
            say(f"{refused} Nothing was changed.")
            return 1
    else:
        before = set()
        chat = ""
        placeholder = _path_of(example)
        text = example.replace(placeholder, str(where), 1) if placeholder else example

    try:
        after = {project.name for project in projects_from_yaml(text)}
    except ValueError as refused:
        say(f"The result would not load, so nothing was changed: {refused}")
        return 1
    if not before | {name} <= after:
        say("The result would lose a project, so nothing was changed.")
        return 1

    backup = back_up(config, now or datetime.now().strftime("%Y%m%d-%H%M%S"))
    config.write_text(text, encoding="utf-8")
    say(f"Added {name} to {config}" + (f" (previous kept at {backup})" if backup else "") + ".")
    say(f"  path: {where}")
    if not backup:
        say("  A new file: put your bot token, group and user id in its `settings:`,")
        say("  and your group in the agent's `chat:` — or run `halyard init`.")
    elif not chat:
        say("  No TELEGRAM_CHAT_ID in the file, so put your group in the agent's `chat:`.")
    say(f"Next: uv run halyard wire {name}")
    return 0


def _sample(named: str) -> Path | None:
    """The sample folder, by its name or its number."""
    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        if named in (folder.name, folder.name[:2]):
            return folder
    return None


def _with_chat(block: dict[str, Any], chat: str) -> dict[str, Any]:
    """The block with every agent speaking in the file's default group."""
    agents = {
        label: {**spec, "chat": chat} if isinstance(spec, dict) else spec
        for label, spec in (block.get("agents") or {}).items()
    }
    return {**block, "agents": agents}


#: `projects:` as a key of its own at the top of the file, with nothing after it
#: on the line but a comment.
_PROJECTS = re.compile(r"^projects:[ \t]*(#.*)?$", re.MULTILINE)


def _with_project(text: str, name: str, block: dict[str, Any]) -> str:
    """`text` with the project added as the first under `projects:`, indented as
    the file indents them — or a `projects:` of its own when there is none."""
    rendered = yaml.safe_dump({name: block}, sort_keys=False, default_flow_style=False)
    found = _PROJECTS.search(text)
    if found is None:
        if re.search(r"^projects:", text, re.MULTILINE):
            raise ValueError("`projects:` is written on one line here, and cannot be added to.")
        indented = "".join(f"  {line}\n" for line in rendered.splitlines())
        return text.rstrip("\n") + "\n\nprojects:\n" + indented
    indent = _indent_after(text, found.end()) or "  "
    indented = "".join(f"{indent}{line}\n" for line in rendered.splitlines())
    return text[: found.end()] + "\n" + indented.rstrip("\n") + text[found.end() :]


def _indent_after(text: str, at: int) -> str:
    """The indentation of the first entry after `at`, or nothing."""
    for line in text[at:].splitlines()[1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            return line[: len(line) - len(line.lstrip())]
    return ""


def _path_of(example: str) -> str:
    """The sample's own placeholder path, as written in its example."""
    found = re.search(r"^\s+path:\s*(\S+)", example, re.MULTILINE)
    return found.group(1) if found else ""
