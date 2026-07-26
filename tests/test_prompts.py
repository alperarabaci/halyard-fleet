"""Prompts you send by name, written in `halyard.yaml`.

A long answer arrives on a phone split into three messages, and handing it on
means copying each piece — while the agent receiving them starts working on the
first third. The way out is not to move the text: ask the agent that has it to
write a file and say where. That sentence is the same every time, so it gets a
name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard.core import prompts


def test_no_block_still_leaves_the_command_working() -> None:
    """A fresh installation has no `prompts:` and should still have `/md`.

    The default deliberately says nothing about where files live in a
    particular repository. That part is what you replace.
    """
    assert "md" in prompts.from_yaml("settings: {}")


def test_the_block_replaces_the_defaults() -> None:
    """Yours, not ours. Editing the wording of a sentence you say every day
    should not mean waiting for a release."""
    found = prompts.from_yaml(
        """
        prompts:
          md: >-
            Put this under NOTES/development-prompts and reply with the path.
        """
    )
    assert found == {"md": "Put this under NOTES/development-prompts and reply with the path."}


def test_a_name_that_cannot_be_a_command_is_refused() -> None:
    """Telegram takes lowercase, digits and underscores, up to 32 characters.

    Refused here rather than discovered when the menu fails to publish — that
    failure is deliberately non-fatal, so the symptom would be a prompt that
    quietly never appears.
    """
    with pytest.raises(ValueError, match="cannot be a command"):
        prompts.from_yaml("prompts:\n  write it up!: hello\n")


def test_a_name_a_built_in_already_uses_is_refused() -> None:
    """One of the two would never run, and which one is not something to find
    out on a phone."""
    with pytest.raises(ValueError, match="built-in command"):
        prompts.from_yaml("prompts:\n  status: tell me everything\n", reserved=["status"])


def test_an_empty_prompt_is_refused() -> None:
    """A command that sends nothing is a command that looks broken."""
    with pytest.raises(ValueError, match="must not be empty"):
        prompts.from_yaml("prompts:\n  md: '   '\n")


def test_the_menu_line_comes_from_the_prompt_itself() -> None:
    """Rather than a second field. A prompt says what it does in its opening
    words — asking for a summary of one sentence is asking for it twice."""
    assert prompts.describe("Write it to a file. Then say where.") == "Write it to a file."


def test_a_menu_line_stays_within_what_telegram_takes() -> None:
    """256 characters, and a description over it makes the whole menu fail to
    publish — taking every other command's label with it."""
    assert len(prompts.describe("word " * 200)) <= 256


def test_a_file_that_cannot_be_parsed_says_so(tmp_path: Path) -> None:
    """Naming the file matters: this one is edited by hand, often."""
    (tmp_path / "halyard.yaml").write_text("prompts: [not, a, mapping]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"halyard\.yaml"):
        prompts.load(tmp_path)
