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
    # One wire for both: with no name it wires every project in the file.
    assert said[-1].startswith("Next: uv run halyard wire — every project")


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


# --- a sample with an inspection -----------------------------------------------


def test_a_sample_s_inspections_go_in_with_the_transition_that_runs_them(
    tmp_path: Path,
) -> None:
    """Sample 04: `report` names `claims` under `inspect:`; without the
    `inspections:` block beside it the project would not load."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    where = tmp_path / "packing-list"
    where.mkdir()

    code, _ = added(config, where, "04", answers=["", "-100888"])

    projects = {p.name: p for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    assert code == 0
    assert projects["packing-list"].inspections == {"claims": Path("inspections/claims.md")}
    assert projects["packing-list"].transitions["report"].inspections == ("claims",)


# --- a sample with commands and trusted runs -----------------------------------


def test_a_sample_s_commands_and_runs_go_in_whole(tmp_path: Path) -> None:
    """Sample 05: commands that take a typed value, a list of them, and the
    agent's trusted runs — each kept as the sample wrote it."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    where = tmp_path / "shopping-cart"
    where.mkdir()

    code, _ = added(config, where, "05")

    [cart] = [
        p
        for p in projects_from_yaml(config.read_text(encoding="utf-8"))
        if p.name == "shopping-cart"
    ]
    assert code == 0
    assert cart.commands == {
        "test-fast": "make test-fast",
        "test-only": "python3 -m unittest -q -k {input.word}",
        "to-main": "git switch main",
        "new-branch": "git switch -c {input.task}",
    }
    assert cart.command_lists == {"next-task": ("to-main", "new-branch")}
    assert cart.runs == ("make test-fast",)


# --- a sample with transitions that run a command ------------------------------


def test_a_sample_s_transitions_go_in_with_the_command_they_run(tmp_path: Path) -> None:
    """Sample 06: `developed` runs `test-fast` before it hands the report on;
    without the command beside it the project would not load."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    where = tmp_path / "cart-transitions"
    where.mkdir()

    code, _ = added(config, where, "06", answers=["", "-100888"])

    projects = {p.name: p for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    cart = projects["cart-transitions"]
    assert code == 0
    assert cart.commands == {"test-fast": "make test-fast"}
    assert cart.transitions["develop"].to == "driver"
    assert cart.transitions["developed"].commands == ("test-fast",)
    assert cart.transitions["developed"].to == "navigator"
    assert {seat.label: seat.chat for seat in cart.seats} == {
        "cart-nav": "-100777",
        "cart-drv": "-100888",
    }


# --- a sample with a workflow --------------------------------------------------


def test_a_sample_s_workflow_goes_in_with_its_steps_and_their_rounds(tmp_path: Path) -> None:
    """Sample 07: the steps, their agents, rounds and `works_on:`, the
    workflow taking them in order, and the inspections `close` runs, each on
    a model of its own — all kept as the sample wrote them."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    where = tmp_path / "cart-workflow"
    where.mkdir()

    code, _ = added(config, where, "07", answers=["", "-100888"])

    projects = {p.name: p for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    cart = projects["cart-workflow"]
    steps = cart.workflows.steps
    assert code == 0
    assert cart.workflows.flows == {"deliver": ("develop", "developed", "close")}
    assert (steps["develop"].seat, steps["develop"].rounds) == ("flow-drv", 2)
    assert (steps["developed"].seat, steps["developed"].rounds) == ("flow-nav", 2)
    assert (steps["close"].seat, steps["close"].works_on) == ("flow-nav", "develop")
    assert cart.transitions["close"].inspections == ("test-reliability", "over-engineering")
    assert cart.transitions["close"].commands == ("test-fast",)
    # Each inspection's own model and effort, which the navigator's turn does
    # not share.
    assert {
        name: (choice.model, choice.effort) for name, choice in cart.inspection_models.items()
    } == {"test-reliability": ("opus", "high"), "over-engineering": ("sonnet", "medium")}


# --- a sample with three agents on two runtimes --------------------------------


def test_three_agents_go_in_each_in_the_group_it_was_given(tmp_path: Path) -> None:
    """Sample 08: two groups of their own asked for and kept, the review the
    navigator acts on, the phases, and an inspection on another runtime's
    model — all as the sample wrote them."""
    config = tmp_path / "halyard.yaml"
    config.write_text(THEIRS, encoding="utf-8")
    where = tmp_path / "shop"
    where.mkdir()

    code, _ = added(config, where, "08", answers=["", "-100888", "-100999"])

    projects = {p.name: p for p in projects_from_yaml(config.read_text(encoding="utf-8"))}
    shop = projects["shop"]
    steps = shop.workflows.steps
    assert code == 0
    assert {(seat.label, seat.runtime, seat.chat) for seat in shop.seats} == {
        ("shop-nav", "claude-code", "-100777"),
        ("shop-drv", "claude-code", "-100888"),
        ("shop-rev", "codex", "-100999"),
    }
    assert shop.workflows.flows == {
        "deliver": ("review", "reviewed", "develop", "developed", "close")
    }
    assert shop.workflows.stretches == {"deliver": (2, 3)}
    assert steps["reviewed"].decided_by == "review"
    assert steps["close"].works_on == "develop"
    assert shop.transitions["review"].followup_prompt == Path("transitions/review-followup.md")
    assert shop.commands_not_run == {"test-fast": (2,)}
    choice = shop.inspection_models["bounded-context"]
    assert (choice.model, choice.effort) == ("gpt-6.1-sol", "high")
