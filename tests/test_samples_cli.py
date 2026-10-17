"""`halyard samples add`: a sample's project into somebody's own halyard.yaml,
without losing anything that was there."""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard import samples_cli
from halyard.core.config_file import projects_from_yaml

#: Somebody's own file: their settings, a project of theirs, their comments.
THEIRS = """\
# My machine.
settings:
  HALYARD_CHANNEL: telegram
  TELEGRAM_BOT_TOKEN: "999:mine"
  TELEGRAM_CHAT_ID: "-100777"
  TELEGRAM_AUTHORIZED_USER_IDS: "42"

projects:
  # The one I work on.
  alpha:
    path: /tmp/alpha
    agents:
      nav:
        runtime: codex
        session: alpha-nav
"""


@pytest.fixture
def copied(tmp_path: Path) -> Path:
    """Where the sample was copied to, as its README says."""
    where = tmp_path / "hello-counter"
    where.mkdir()
    return where


def added(config: Path, where: Path, sample: str = "01") -> tuple[int, list[str]]:
    said: list[str] = []
    code = samples_cli.add(sample, where, config=config, now="20261017-201500", say=said.append)
    return code, said


def test_the_sample_is_added_and_everything_already_there_is_kept(
    tmp_path: Path, copied: Path
) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    code, _ = added(config, copied)

    text = config.read_text(encoding="utf-8")
    projects = {project.name: project for project in projects_from_yaml(text)}
    assert code == 0
    assert set(projects) == {"alpha", "hello-counter"}
    assert projects["hello-counter"].path == copied
    # The file's own default group, not the sample's placeholder.
    assert projects["hello-counter"].seats[0].chat == "-100777"
    # Their comments and their project, as they were.
    assert "# My machine." in text and "# The one I work on." in text
    assert text.count('TELEGRAM_BOT_TOKEN: "999:mine"') == 1


def test_the_file_is_backed_up_before_it_is_written(tmp_path: Path, copied: Path) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    added(config, copied)

    backup = tmp_path / "halyard.yaml.20261017-201500.bak"
    assert backup.read_text(encoding="utf-8") == THEIRS


def test_a_project_of_the_same_name_is_left_alone(tmp_path: Path, copied: Path) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    added(config, copied)
    once = config.read_text(encoding="utf-8")

    code, said = added(config, copied)

    assert code == 1
    assert "already in" in said[0]
    assert config.read_text(encoding="utf-8") == once


def test_a_file_with_no_projects_gets_them(tmp_path: Path, copied: Path) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS.split("\nprojects:")[0] + "\n", encoding="utf-8")

    code, _ = added(config, copied)

    assert code == 0
    assert [p.name for p in projects_from_yaml(config.read_text(encoding="utf-8"))] == [
        "hello-counter"
    ]


def test_projects_written_on_one_line_are_not_guessed_at(tmp_path: Path, copied: Path) -> None:
    config = tmp_path / "halyard.yaml"
    written = "settings:\n  HALYARD_CHANNEL: telegram\nprojects: {}\n"
    config.write_text(written, encoding="utf-8")

    code, _ = added(config, copied)

    assert code == 1
    assert config.read_text(encoding="utf-8") == written
    assert not (tmp_path / "halyard.yaml.20261017-201500.bak").exists()


def test_with_no_file_the_sample_s_example_is_the_file(tmp_path: Path, copied: Path) -> None:
    config = tmp_path / "halyard.yaml"

    code, said = added(config, copied)

    [project] = projects_from_yaml(config.read_text(encoding="utf-8"))
    assert code == 0
    assert project.path == copied
    assert any("bot token" in line for line in said)


def test_a_sample_left_inside_the_checkout_is_refused(tmp_path: Path) -> None:
    """Wired there, it would gate Halyard's own repository."""
    config = tmp_path / "halyard.yaml"

    code, said = added(config, samples_cli.SAMPLES / "01-one-agent")

    assert code == 2
    assert "inside this Halyard checkout" in said[0]
    assert not config.exists()


def test_a_path_not_copied_to_yet_and_an_unknown_sample_are_said(tmp_path: Path) -> None:
    config = tmp_path / "halyard.yaml"

    missing, said_missing = added(config, tmp_path / "nowhere")
    unknown, said_unknown = added(config, tmp_path, sample="99")

    assert (missing, unknown) == (2, 2)
    assert "Copy the sample to it first" in said_missing[0]
    assert "01-one-agent" in said_unknown[0]
