"""Prompts you send by name, written in `halyard.yaml`.

The thing a phone is worst at is moving text from one place to another. A long
answer arrives split into three messages, because Telegram will not carry more
than about four thousand characters at once, and handing those on means copying
each piece — while the agent on the receiving end reads the first one, decides a
message has arrived, and starts working on a third of the instruction.

The way out is not to move the text at all. Ask the agent that already has it to
write it to a file and say where, and what travels is a path.

That is one sentence, typed the same way every time, which is what a command is
for:

    prompts:
      md: >-
        Write what you just produced to a Markdown file in this repository,
        somewhere that fits, and reply with the path and nothing else.

Each key becomes a command — `/md` here — and the text is sent into the session
that chat belongs to. Anything typed after the command is appended, so `/md the
failing test` reaches the agent with that on the end.

**They are yours, not ours.** The default below exists so the command works on a
fresh installation; the shape exists so nobody has to ask for a release to
change the wording of a sentence they say every day.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

import yaml

#: What Telegram accepts as a command name: lowercase, digits, underscores, at
#: most 32 characters. Checked here rather than discovered when the menu fails
#: to publish, because that failure is deliberately non-fatal and would be a
#: prompt that quietly never appears.
NAME = re.compile(r"^[a-z0-9_]{1,32}$")

#: Telegram's bounds on a command description in the menu.
_SHORTEST, _LONGEST = 3, 256

#: Enough to make `/md` work before anybody has written a `prompts:` block.
#: Deliberately says nothing about where files live in a particular repository —
#: that part is what you are expected to replace.
DEFAULTS: dict[str, str] = {
    "md": (
        "Write what you just produced to a Markdown file in this repository, "
        "somewhere that fits, and reply with the path and nothing else."
    ),
}


def describe(prompt: str) -> str:
    """A menu line for a prompt, taken from its own first sentence.

    Rather than a second field to fill in. A prompt says what it does in its
    opening words — that is what makes it a prompt — and asking for a summary
    of one sentence is asking somebody to write it twice.
    """
    first = re.split(r"(?<=[.!?])\s", " ".join(prompt.split()), maxsplit=1)[0]
    if len(first) < _SHORTEST:
        return "Configured prompt"
    return first if len(first) <= _LONGEST else first[: _LONGEST - 1] + "…"


def from_yaml(text: str, reserved: Collection[str] = ()) -> dict[str, str]:
    """The `prompts:` block, or the defaults when there is none.

    Refuses what it cannot honour, in keeping with the seats beside it: a
    prompt bound to a name that already means something else would be a command
    that does one thing on a phone and another in the file describing it.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None

    block: Any = loaded.get("prompts") if isinstance(loaded, dict) else None
    if block is None:
        return dict(DEFAULTS)
    if not isinstance(block, dict):
        raise ValueError("`prompts:` must be a mapping of command name to the text to send.")

    taken = {str(name).lower() for name in reserved}
    found: dict[str, str] = {}
    for name, body in block.items():
        name = str(name).strip().lower()
        if not NAME.match(name):
            raise ValueError(
                f"Prompt name {name!r} cannot be a command. Use lowercase letters, "
                "digits and underscores, up to 32 characters."
            )
        if name in taken:
            raise ValueError(
                f"Prompt {name!r} has the same name as a built-in command, so one of "
                "them would never run. Pick another name."
            )
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"Prompt {name!r} must be the text to send, and it must not be empty.")
        found[name] = body.strip()
    return found


def load(directory: Path | None = None, reserved: Collection[str] = ()) -> dict[str, str]:
    """Prompts from the configuration, or the defaults if there is no file."""
    from halyard.core.config_file import find_config

    path = find_config(directory)
    if path is None:
        return dict(DEFAULTS)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    try:
        return from_yaml(text, reserved)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
