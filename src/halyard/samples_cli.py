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

A sample's prompts go in too: `/sample_01` in the group sends its task. A prompt
of the same name already there is kept as it is. A file with no `prompts:` yet
also gets the default `/md`, which a block of its own would otherwise drop.

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

from halyard.core import prompts as configured_prompts
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
    shipped = yaml.safe_load(example)
    [(name, block)] = (shipped.get("projects") or {}).items()
    block = {**block, "path": str(where)}
    wanted: dict[str, str] = shipped.get("prompts") or {}
    kept: list[str] = []

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
            text = _with_entry(text, "projects", name, block)
            text, kept = _with_prompts(text, written, wanted)
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
    try:
        offered = configured_prompts.from_yaml(text)
    except ValueError as refused:
        say(f"The result's prompts would not load, so nothing was changed: {refused}")
        return 1

    backup = back_up(config, now or datetime.now().strftime("%Y%m%d-%H%M%S"))
    config.write_text(text, encoding="utf-8")
    say(f"Added {name} to {config}" + (f" (previous kept at {backup})" if backup else "") + ".")
    say(f"  path: {where}")
    for prompt in wanted:
        if prompt in offered:
            say(f"  /{prompt} in the group sends this sample's task.")
    for line in kept:
        say(f"  {line}")
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


def _with_prompts(text: str, written: dict, wanted: dict[str, str]) -> tuple[str, list[str]]:
    """`text` with the sample's prompts added, and what was kept instead.

    A prompt of the same name is somebody's own and stays as it is. With no
    prompts written yet, the defaults go in too: a `prompts:` block of its own
    replaces them, and `/md` would quietly stop working.
    """
    have = written.get("prompts") if isinstance(written, dict) else None
    if have is not None and not isinstance(have, dict):
        raise ValueError("`prompts:` is not a mapping here, and cannot be added to.")
    have = have or {}
    adding = {} if have else dict(configured_prompts.DEFAULTS)
    kept: list[str] = []
    for prompt, body in wanted.items():
        if prompt in have:
            if str(have[prompt]).strip() != str(body).strip():
                kept.append(f"/{prompt} is already a prompt of yours, and was kept as it is.")
            continue
        adding[prompt] = body
    for prompt, body in adding.items():
        text = _with_entry(text, "prompts", prompt, body)
    return text, kept


def _with_entry(text: str, section: str, name: str, value: Any) -> str:
    """`text` with `name` added as the first entry under the top-level `section:`,
    indented as the file indents its entries — or a `section:` of its own at the
    end when there is none. A section written on one line is refused: adding to
    it is a guess about somebody's file."""
    rendered = yaml.safe_dump(
        {name: value}, sort_keys=False, default_flow_style=False, allow_unicode=True, width=88
    )
    found = re.search(rf"^{section}:[ \t]*(#.*)?$", text, re.MULTILINE)
    if found is None:
        if re.search(rf"^{section}:", text, re.MULTILINE):
            raise ValueError(f"`{section}:` is written on one line here, and cannot be added to.")
        indented = "".join(f"  {line}\n" for line in rendered.splitlines())
        return text.rstrip("\n") + f"\n\n{section}:\n" + indented
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
