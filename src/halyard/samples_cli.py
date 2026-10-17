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

A sample with several projects is copied whole, and `<path>` is where the copy
is: each project is the folder of its name inside it, as its example lays them
out.

The sample's own `chat:` values are placeholders, so it asks which group each
agent speaks in. One written as the example's `TELEGRAM_CHAT_ID` offers the
file's default group; one written as another id wants a group of its own, which
`/status` in that group names. A group another agent already speaks in is
refused, and so is the same group twice: a typed message there reaches only one
of them.

With no `halyard.yaml` yet, the sample's example is the file — its settings still
placeholders, to fill in or to replace by running `halyard init`.
"""

from __future__ import annotations

import os
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

  Adds the sample's projects to halyard.yaml, with <path> as where it is,
  and asks which Telegram group each agent speaks in.
  <sample> is a folder under samples/, or its number: 01-one-agent, or 01.
  Copy the sample to <path> first; its README says how.
"""

Say = Callable[[str], None]
Ask = Callable[[str, str], str]

#: What a group's id looks like: negative, with a forum topic after a colon.
_GROUP = re.compile(r"^-\d+(:\d+)?$")


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
    ask: Ask | None = None,
) -> int:
    """Add `sample`'s projects to `config`, copied to `where`. 0 when they were added."""
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
    blocks: dict[str, dict] = shipped.get("projects") or {}
    placed = _placed(blocks, where)
    if missing := [path for path in placed.values() if not path.is_dir()]:
        say(f"{missing[0]} is not there. Copy the whole sample to {where}; its README says how.")
        return 2
    blocks = {name: {**block, "path": str(placed[name])} for name, block in blocks.items()}
    wanted: dict[str, str] = shipped.get("prompts") or {}
    kept: list[str] = []
    chats: dict[tuple[str, str], str] = {}

    if config.exists():
        text = config.read_text(encoding="utf-8")
        written = yaml.safe_load(text) or {}
        before = set((written.get("projects") or {}) if isinstance(written, dict) else {})
        if taken := [name for name in blocks if name in before]:
            verb = "is" if len(taken) == 1 else "are"
            say(f"{', '.join(taken)} {verb} already in {config}; nothing was changed.")
            return 1
        try:
            speaking = _speaking(text)
        except ValueError as refused:
            say(f"{config} does not load, so nothing was changed: {refused}")
            return 1
        default = str(((written.get("settings") or {}).get("TELEGRAM_CHAT_ID")) or "")
        asked = _chats(shipped, default, speaking, ask or _ask)
        if isinstance(asked, str):
            say(f"{asked} Nothing was changed.")
            return 1
        chats = asked
        try:
            for name, block in reversed(blocks.items()):
                text = _with_entry(text, "projects", name, _with_chats(name, block, chats))
            text, kept = _with_prompts(text, written, wanted)
        except ValueError as refused:
            say(f"{refused} Nothing was changed.")
            return 1
    else:
        before = set()
        text = example
        for name, block in shipped["projects"].items():
            text = _with_path(text, str(block.get("path") or ""), placed[name])

    try:
        after = {project.name for project in projects_from_yaml(text)}
    except ValueError as refused:
        say(f"The result would not load, so nothing was changed: {refused}")
        return 1
    if not before | set(blocks) <= after:
        say("The result would lose a project, so nothing was changed.")
        return 1
    try:
        offered = configured_prompts.from_yaml(text)
    except ValueError as refused:
        say(f"The result's prompts would not load, so nothing was changed: {refused}")
        return 1

    backup = back_up(config, now or datetime.now().strftime("%Y%m%d-%H%M%S"))
    config.write_text(text, encoding="utf-8")
    kept_at = f" (previous kept at {backup})" if backup else ""
    say(f"Added {', '.join(blocks)} to {config}{kept_at}.")
    for name in blocks:
        say(f"  {name}: {placed[name]}")
        for (project, label), chat in chats.items():
            if project == name:
                say(f"    {label} speaks in {chat}")
    for prompt in wanted:
        if prompt in offered:
            say(f"  /{prompt} in an agent's group sends it this sample's task.")
    for line in kept:
        say(f"  {line}")
    if not backup:
        say("  A new file: put your bot token, group and user id in its `settings:`,")
        say("  and each agent's group in its `chat:` — or run `halyard init`.")
    say("Next:")
    for name in blocks:
        say(f"  uv run halyard wire {name}")
    return 0


def _placed(blocks: dict[str, dict], where: Path) -> dict[str, Path]:
    """Where each project is, once the sample is copied to `where`.

    One project is the copy itself. Several are laid out under the folder
    their example paths share, and keep that layout in the copy.
    """
    written = {name: Path(str(block.get("path") or name)) for name, block in blocks.items()}
    if len(written) == 1:
        return dict.fromkeys(written, where)
    shared = Path(os.path.commonpath([str(path) for path in written.values()]))
    return {name: where / path.relative_to(shared) for name, path in written.items()}


def _speaking(text: str) -> dict[str, str]:
    """Which agent already speaks in each group, by the group's id.

    By the group alone: a message is routed by the chat it was typed in, so
    two topics of one group are one place to type into.
    """
    return {
        seat.chat.split(":")[0]: f"{project.name}'s {seat.label}"
        for project in projects_from_yaml(text)
        for seat in project.seats
        if seat.chat
    }


def _chats(
    shipped: dict, default: str, speaking: dict[str, str], ask: Ask
) -> dict[tuple[str, str], str] | str:
    """The group each of the sample's agents speaks in, asked for — or why not.

    An agent written in the example's default group is offered the file's.
    Any other wants a group of its own, and has no default to offer.
    """
    placeholder = str((shipped.get("settings") or {}).get("TELEGRAM_CHAT_ID") or "")
    chats: dict[tuple[str, str], str] = {}
    taken = dict(speaking)
    for name, block in (shipped.get("projects") or {}).items():
        for label, spec in (block.get("agents") or {}).items():
            if not isinstance(spec, dict) or not spec.get("chat"):
                continue
            own = str(spec["chat"]) != placeholder
            offered = "" if own else default
            question = (
                f"Group for {name}'s {label} ({spec.get('runtime')}), a group of its own"
                if own
                else f"Group for {name}'s {label} ({spec.get('runtime')})"
            )
            chat = ask(question, offered).strip()
            if not chat:
                return (
                    f"No group for {name}'s {label}. Make one, add the bot, send /status "
                    "in it for its id, and run this again."
                )
            if not _GROUP.match(chat):
                return f"{chat} is not a group's id, which starts with a minus: -100…"
            if (other := taken.get(chat.split(":")[0])) is not None:
                return (
                    f"{chat} is where {other} speaks already, and a message typed there "
                    f"reaches only one agent. Make a group for {name}'s {label}, send "
                    "/status in it for its id, and run this again."
                )
            taken[chat.split(":")[0]] = f"{name}'s {label}"
            chats[(name, label)] = chat
    return chats


def _ask(question: str, default: str) -> str:
    shown = f"{question} [{default}]: " if default else f"{question}: "
    try:
        return input(shown).strip() or default
    except EOFError:
        return default


def _sample(named: str) -> Path | None:
    """The sample folder, by its name or its number."""
    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        if named in (folder.name, folder.name[:2]):
            return folder
    return None


def _with_chats(name: str, block: dict[str, Any], chats: dict[tuple[str, str], str]) -> dict:
    """The block with each agent speaking in the group it was given."""
    agents = {
        label: {**spec, "chat": chats[(name, label)]} if (name, label) in chats else spec
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


def _with_path(text: str, placeholder: str, path: Path) -> str:
    """The example's text with one project's placeholder path made real."""
    return re.sub(
        rf"^(\s+path:[ \t]*){re.escape(placeholder)}[ \t]*$",
        lambda found: f"{found.group(1)}{path}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
