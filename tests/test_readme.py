"""The README describes the Halyard that is here: every command the bot answers,
and a team configuration Halyard reads as written."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_readme_names_every_command_the_bot_answers() -> None:
    """A command the table leaves out is one a reader never learns to press —
    `/workflow` was missing from it for weeks while workflows shipped."""
    from halyard.channels.telegram.adapter import COMMANDS, GROUPS, UNLISTED

    text = README.read_text(encoding="utf-8")
    table = text.split("| In Telegram |", 1)[1].split("\n\n", 1)[0]
    named = set(re.findall(r"`/([a-z_]+)`", table))

    answered = {name for name, _ in COMMANDS + GROUPS} | set(UNLISTED)
    assert answered - named == set()


def test_the_team_the_readme_shows_is_one_halyard_reads() -> None:
    """A flow naming a step, a step naming a transition or an agent the project
    does not define is refused when the file is read — so the README's team
    is read here as Halyard reads it."""
    from halyard.core.config_file import projects_from_yaml

    text = README.read_text(encoding="utf-8")
    [block] = re.findall(r"### A team, in one file\n.*?```yaml\n(.*?)```", text, re.DOTALL)

    [project] = projects_from_yaml(block)

    assert set(project.transitions) == {"develop", "developed", "close"}
    assert set(project.inspections) == {"test-reliability", "over-engineering"}
    assert {seat.role for seat in project.seats} == {"navigator", "driver"}
