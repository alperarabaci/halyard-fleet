"""The README, and the docs it sends people to, describe the Halyard that is
here: every command the bot answers, a team configuration Halyard reads as
written, and links that lead somewhere."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def test_the_command_reference_names_every_command_the_bot_answers() -> None:
    """A command the table leaves out is one a reader never learns to press —
    `/workflow` was missing from it for weeks while workflows shipped."""
    from halyard.channels.telegram.adapter import COMMANDS, GROUPS, UNLISTED

    text = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
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


def _anchor(heading: str) -> str:
    """The fragment GitHub gives a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_every_link_in_the_readme_and_the_docs_leads_somewhere() -> None:
    """The README is mostly a way to somewhere else, so a link that leads
    nowhere — a file moved, a heading renamed — is the README failing at the
    one thing it is for."""
    for page in [README, *sorted((ROOT / "docs").rglob("*.md"))]:
        text = re.sub(r"```.*?```", "", page.read_text(encoding="utf-8"), flags=re.DOTALL)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"https?:|mailto:", target):
                continue
            path, _, fragment = target.partition("#")
            destination = (page.parent / path).resolve() if path else page
            where = f"{page.relative_to(ROOT)} links to {target}"
            assert destination.exists(), where
            if fragment and destination.suffix == ".md":
                headings = re.findall(r"^#+ (.+)$", destination.read_text(encoding="utf-8"), re.M)
                assert fragment in {_anchor(heading) for heading in headings}, where
