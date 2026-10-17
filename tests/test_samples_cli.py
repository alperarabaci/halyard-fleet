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


def added(
    config: Path, where: Path, sample: str = "01", *, answers: list[str] | None = None
) -> tuple[int, list[str]]:
    """The sample added, each group it asks for answered in turn — Enter, which
    takes what it offers, when no answers are given."""
    said: list[str] = []
    given = list(answers or [])

    def ask(question: str, offered: str) -> str:
        said.append(f"? {question} [{offered}]")
        return (given.pop(0) if given else "") or offered

    code = samples_cli.add(
        sample, where, config=config, now="20261017-201500", say=said.append, ask=ask
    )
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
    assert "is already in" in said[-1]
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


def test_the_sample_s_prompt_goes_in_and_md_stays(tmp_path: Path, copied: Path) -> None:
    """A `prompts:` block of its own replaces the defaults, so a file that had
    none gets `/md` too — otherwise it would quietly stop working."""
    from halyard.core import prompts

    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    code, said = added(config, copied)

    offered = prompts.from_yaml(config.read_text(encoding="utf-8"))
    assert code == 0
    assert set(offered) == {"md", "sample_01"}
    assert offered["sample_01"].startswith('Add a "Reset" button')
    assert any("/sample_01 in an agent's group" in line for line in said)


def test_their_own_prompts_are_kept_and_a_name_they_use_is_not_taken(
    tmp_path: Path, copied: Path
) -> None:
    from halyard.core import prompts

    config = tmp_path / "halyard.yaml"
    config.write_text(
        THEIRS + "\nprompts:\n  md: Mine.\n  sample_01: Something else of mine.\n",
        encoding="utf-8",
    )

    code, said = added(config, copied)

    offered = prompts.from_yaml(config.read_text(encoding="utf-8"))
    assert code == 0
    assert offered == {"md": "Mine.", "sample_01": "Something else of mine."}
    assert any("kept as it is" in line for line in said)


# --- a sample with two projects ------------------------------------------------


@pytest.fixture
def two(tmp_path: Path) -> Path:
    """Sample 02 copied out: a folder with a project in each of its folders."""
    where = tmp_path / "two-agents"
    for product in ("greeting-page", "todo-list"):
        (where / product).mkdir(parents=True)
    return where


def test_two_projects_go_in_at_their_folders_each_agent_in_the_group_it_was_given(
    tmp_path: Path, two: Path
) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    code, said = added(config, two, "02", answers=["", "-100888"])

    projects = {p.name: p for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    assert code == 0
    assert set(projects) == {"alpha", "greeting-page", "todo-list"}
    assert projects["greeting-page"].path == two / "greeting-page"
    assert projects["todo-list"].path == two / "todo-list"
    # Enter keeps the default group for the agent written in it; the other is
    # given what was typed.
    assert projects["greeting-page"].seats[0].chat == "-100777"
    assert projects["todo-list"].seats[0].chat == "-100888"
    assert projects["todo-list"].commands == {"check": "./check.sh"}
    assert any("wire todo-list" in line for line in said)


def test_an_agent_that_wants_a_group_of_its_own_is_offered_none(tmp_path: Path, two: Path) -> None:
    """Offering the default group there would put two agents in it."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    code, said = added(config, two, "02")

    assert code == 1
    assert "? Group for todo-list's todo-dev (codex), a group of its own []" in said
    assert "No group for todo-list's todo-dev" in said[-1]
    assert config.read_text(encoding="utf-8") == THEIRS
    assert not (tmp_path / "halyard.yaml.20261017-201500.bak").exists()


@pytest.mark.parametrize(
    ("answers", "refused"),
    [
        # Another project's agent speaks there already.
        (["-100555"], "is where alpha's nav speaks already"),
        # A topic of that group is the same place to type into.
        (["-100555:7"], "is where alpha's nav speaks already"),
        (["12345"], "is not a group's id"),
    ],
)
def test_a_group_that_would_not_reach_the_agent_is_refused(
    tmp_path: Path, copied: Path, answers: list[str], refused: str
) -> None:
    config = tmp_path / "halyard.yaml"
    theirs = THEIRS + '        chat: "-100555"\n'
    config.write_text(theirs, encoding="utf-8")

    code, said = added(config, copied, answers=answers)

    assert code == 1
    assert refused in said[-1]
    assert config.read_text(encoding="utf-8") == theirs


def test_the_same_group_for_both_agents_is_refused(tmp_path: Path, two: Path) -> None:
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")

    code, said = added(config, two, "02", answers=["", "-100777"])

    assert code == 1
    assert "is where greeting-page's greeting-dev speaks already" in said[-1]
    assert config.read_text(encoding="utf-8") == THEIRS


def test_a_project_folder_missing_from_the_copy_is_said(tmp_path: Path) -> None:
    config = tmp_path / "halyard.yaml"
    half = tmp_path / "two-agents"
    (half / "greeting-page").mkdir(parents=True)

    code, said = added(config, half, "02")

    assert code == 2
    assert "todo-list is not there" in said[0]
    assert not config.exists()


def test_with_no_file_both_paths_are_made_real(tmp_path: Path, two: Path) -> None:
    config = tmp_path / "halyard.yaml"

    code, _ = added(config, two, "02")

    projects = {p.name: p.path for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    assert code == 0
    assert projects == {"greeting-page": two / "greeting-page", "todo-list": two / "todo-list"}
