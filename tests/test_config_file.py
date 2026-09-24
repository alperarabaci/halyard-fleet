"""The YAML configuration: projects, and the seats sitting in them.

The shared contract in `test_seat_contract.py` already proves this producer
agrees with the environment one about what a seat is. What is here is what YAML
can express and the environment cannot — a project, its path, and more than one
of them — plus the precedence rule between the two files.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from halyard.core import seats as seats_module
from halyard.core.config_file import (
    find_config,
    from_yaml,
    load,
    projects_from_yaml,
    resolve_project,
)
from halyard.core.events import Role

TWO_PROJECTS = """
projects:
  alpha-engine:
    path: ~/code/alpha-engine
    seats:
      nav:  {runtime: claude-code, session: alpha-navigator, chat: "-1001", role: navigator}
      xdrv: {runtime: codex,       session: alpha-xdriver,   chat: "-1004", role: driver}
  hermes:
    path: /srv/hermes
    seats:
      hnav: {runtime: codex, session: hermes-nav, chat: "-2001", role: navigator}
"""


def test_seats_know_which_project_they_belong_to() -> None:
    """The reason for the file. A flat list cannot say this at all."""
    found = from_yaml(TWO_PROJECTS)

    assert [(s.label, s.project) for s in found] == [
        ("nav", "alpha-engine"),
        ("xdrv", "alpha-engine"),
        ("hnav", "hermes"),
    ]


def test_more_than_one_project_is_just_another_block() -> None:
    projects = projects_from_yaml(TWO_PROJECTS)

    assert [p.name for p in projects] == ["alpha-engine", "hermes"]
    assert [len(p.seats) for p in projects] == [2, 1]


def test_a_project_carries_where_to_wire_it() -> None:
    """`~` expanded here rather than by whoever reads it later — a path that
    works in a shell and not in the process is the kind of difference nobody
    looks for."""
    projects = projects_from_yaml(TWO_PROJECTS)

    assert projects[0].path == Path.home() / "code/alpha-engine"
    assert projects[1].path == Path("/srv/hermes")


def test_seat_order_follows_the_file_not_the_alphabet() -> None:
    """Order is what somebody wrote, and `doctor` lists seats in it."""
    found = from_yaml(
        """
projects:
  p:
    seats:
      zulu:  {runtime: codex, session: z}
      alpha: {runtime: codex, session: a}
"""
    )

    assert [s.label for s in found] == ["zulu", "alpha"]


def test_an_unquoted_chat_id_is_still_a_chat_id() -> None:
    """`-1001` is a number to YAML and a string everywhere else.

    Left alone the mismatch does not fail — the seat simply routes nowhere,
    quietly, which is the worst available outcome and exactly the shape of bug
    this project keeps finding.
    """
    found = from_yaml("projects:\n  p:\n    seats:\n      s: {runtime: codex, chat: -1001}\n")

    assert found[0].chat == "-1001"


def test_a_duplicate_label_across_projects_is_refused() -> None:
    """Labels name a seat in `doctor` and find it in `find`. Two of them makes
    one unreachable and says nothing about it."""
    with pytest.raises(ValueError, match="unique across projects"):
        from_yaml(
            """
projects:
  one:
    seats: {nav: {runtime: codex, session: a}}
  two:
    seats: {nav: {runtime: codex, session: b}}
"""
        )


def test_a_project_with_no_seats_is_allowed() -> None:
    """A codebase can be described before anybody decides to gate it."""
    projects = projects_from_yaml("projects:\n  someday:\n    path: /tmp/someday\n")

    assert projects[0].seats == []


def test_an_empty_document_is_no_seats_rather_than_an_error() -> None:
    assert from_yaml("") == []
    assert from_yaml("projects:\n") == []


def test_something_that_is_not_a_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="mapping"):
        from_yaml("- just\n- a list\n")


def test_broken_yaml_says_so_rather_than_loading_half_of_it() -> None:
    with pytest.raises(ValueError, match="Could not read"):
        from_yaml("projects:\n  p:\n   seats: {oops\n")


# --- which file wins ---------------------------------------------------------


def test_yaml_is_found_beside_the_env_file(tmp_path: Path) -> None:
    (tmp_path / "halyard.yaml").write_text(TWO_PROJECTS)

    assert find_config(tmp_path) == tmp_path / "halyard.yaml"


def test_the_yml_spelling_is_found_too(tmp_path: Path) -> None:
    (tmp_path / "halyard.yml").write_text(TWO_PROJECTS)

    assert find_config(tmp_path) is not None


def test_no_file_is_not_an_error(tmp_path: Path) -> None:
    """The environment dialect stays a complete way to configure this."""
    assert find_config(tmp_path) is None
    assert load(tmp_path) == []


def test_yaml_wins_outright_and_the_two_are_never_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging would answer "which file is this seat from?" with "both, partly".

    A seat somebody thought they had replaced would still be routing, and
    nothing anywhere would say which file it came from.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HALYARD_SEATS", "from-env")
    monkeypatch.setenv("HALYARD_SEAT_FROM_ENV", "runtime=codex session=env-session chat=-1")
    (tmp_path / "halyard.yaml").write_text(
        "projects:\n  p:\n    seats:\n      from-yaml: {runtime: codex, session: yaml-session}\n"
    )

    found = seats_module.configured(tmp_path)

    assert [s.label for s in found] == ["from-yaml"]


def test_the_environment_is_used_when_there_is_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HALYARD_SEATS", "from-env")
    monkeypatch.setenv("HALYARD_SEAT_FROM_ENV", "runtime=codex session=env-session chat=-1")

    found = seats_module.configured(tmp_path)

    assert [s.label for s in found] == ["from-env"]


def test_a_file_that_cannot_be_read_raises_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back would start the control plane holding a configuration
    nobody wrote — worse than not starting."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HALYARD_SEATS", "from-env")
    monkeypatch.setenv("HALYARD_SEAT_FROM_ENV", "runtime=codex session=s")
    (tmp_path / "halyard.yaml").write_text("projects:\n  p:\n    seats: {s: {runtime: nope}}\n")

    with pytest.raises(ValueError, match=r"halyard\.yaml"):
        seats_module.configured(tmp_path)


def test_a_role_is_optional_and_two_seats_may_share_one() -> None:
    """Two drivers is the arrangement this whole design exists for."""
    found = from_yaml(
        """
projects:
  p:
    seats:
      drv:  {runtime: claude-code, session: a, chat: "-1", role: driver}
      xdrv: {runtime: codex,       session: b, chat: "-2", role: driver}
      spare: {runtime: codex, session: c}
"""
    )

    assert [s.role for s in found] == [Role.DRIVER, Role.DRIVER, None]


# --- wiring by project name --------------------------------------------------


def test_a_project_name_resolves_to_its_directory(tmp_path: Path) -> None:
    """So `halyard wire alpha-engine` works.

    The file already says where every project is, and retyping the path is both
    tedious and a way to gate the wrong tree — which looks like success right
    up until a command runs somewhere nobody was watching.
    """
    (tmp_path / "alpha").mkdir()
    (tmp_path / "halyard.yaml").write_text(
        f"projects:\n  alpha-engine:\n    path: {tmp_path / 'alpha'}\n"
        "    seats:\n      nav:\n        runtime: codex\n        session: s\n"
    )

    assert resolve_project("alpha-engine", tmp_path) == tmp_path / "alpha"


def test_a_project_name_is_matched_however_it_is_typed(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "halyard.yaml").write_text(
        f"projects:\n  Alpha-Engine:\n    path: {tmp_path / 'alpha'}\n"
    )

    assert resolve_project("  alpha-engine  ", tmp_path) is not None


def test_a_project_with_no_path_says_that_rather_than_guessing(tmp_path: Path) -> None:
    """A described project and an unlocatable one are different mistakes.

    Guessing a directory here would gate whatever happened to be nearby.
    """
    (tmp_path / "halyard.yaml").write_text(
        "projects:\n  alpha-engine:\n    seats:\n      nav:\n        runtime: codex\n"
    )

    with pytest.raises(ValueError, match="no `path:`"):
        resolve_project("alpha-engine", tmp_path)


def test_a_path_that_is_not_there_is_reported_as_that(tmp_path: Path) -> None:
    """Distinct from both of the above: the file is right and the machine is
    not — a checkout that lives somewhere else, or has not been cloned yet."""
    (tmp_path / "halyard.yaml").write_text(
        "projects:\n  alpha-engine:\n    path: /nowhere/at/all\n"
    )

    with pytest.raises(ValueError, match="not a directory"):
        resolve_project("alpha-engine", tmp_path)


def test_an_unknown_project_lists_the_ones_there_are(tmp_path: Path) -> None:
    """The next thing anybody asks is "what did I call it?"."""
    (tmp_path / "halyard.yaml").write_text(
        "projects:\n  alpha-engine:\n    path: /tmp\n  hermes:\n    path: /tmp\n"
    )

    with pytest.raises(ValueError) as refused:
        resolve_project("alfa-engine", tmp_path)

    assert "alpha-engine" in str(refused.value)
    assert "hermes" in str(refused.value)


def test_no_configuration_at_all_says_what_to_do_instead(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Give a directory instead"):
        resolve_project("alpha-engine", tmp_path)


def test_a_broken_configuration_says_which_file_and_why(tmp_path, monkeypatch, caplog) -> None:
    """The measured failure, on a Mac mini being configured for a new release.

    A mistyped line of YAML read as "no configuration at all", and what came
    next was pydantic reporting `HALYARD_CHANNEL` missing — true, and pointing
    at the wrong thing entirely. The service restarted 553 times, never started,
    and said nothing about the file it could not read.
    """
    import logging

    from halyard.config import Settings

    (tmp_path / "halyard.yaml").write_text("settings:\n  HALYARD_CHANNEL: telegram\n  x: [1, 2\n")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.ERROR), contextlib.suppress(Exception):
        Settings()

    said = [record.getMessage() for record in caplog.records]
    assert any("halyard.yaml" in one and "no settings were loaded" in one for one in said)
    # Once, not once per field: a reason said twenty-eight times is scrolled past.
    assert sum("no settings were loaded" in one for one in said) == 1


# --- the files a configuration names ----------------------------------------


def a_project(tmp_path, **body) -> list:
    """One project on disk, configured as the YAML would write it."""
    from halyard.core.config_file import projects_from_yaml

    code = tmp_path / "alpha-engine"
    code.mkdir(exist_ok=True)
    written = "\n".join(f"    {line}" for line in body.pop("lines", []))
    return projects_from_yaml(
        f"projects:\n  alpha-engine:\n    path: {code}\n{written}\n"
        "    seats:\n      nav: {runtime: claude-code}\n"
    )


def test_a_configuration_whose_files_are_all_there_says_nothing(tmp_path) -> None:
    """Empty is the answer worth being able to get in one look."""
    from halyard.core.config_file import missing_files

    (tmp_path / "alpha-engine").mkdir()
    (tmp_path / "alpha-engine" / "REVIEW.md").write_text("# the round")

    found = a_project(tmp_path, lines=["confirmation:", "  review: REVIEW.md"])

    assert missing_files(found) == []


def test_a_file_that_is_not_there_is_named_with_its_setting(tmp_path) -> None:
    """The measured failure: a Mac mini ran for weeks with none of its prompt
    files present. They were named in `halyard.yaml`, read at the moment they
    were needed, and their absence produced a warning nobody was reading."""
    from halyard.core.config_file import missing_files

    found = a_project(tmp_path, lines=["confirmation:", "  review: NOTES/GONE.md"])

    said = missing_files(found)
    assert len(said) == 1
    assert "alpha-engine" in said[0]
    assert "confirmation.review" in said[0]
    assert "NOTES/GONE.md" in said[0]


def test_inspections_are_read_as_names_and_files(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "inspections:",
            "  proof: NOTES/inspections/proof.md",
            "  claims: NOTES/inspections/claims.md",
        ],
    )

    assert project.inspections == {
        "proof": Path("NOTES/inspections/proof.md"),
        "claims": Path("NOTES/inspections/claims.md"),
    }
    assert project.older == ()


def test_the_name_inspections_had_before_is_still_read_and_said(tmp_path) -> None:
    """`checks:` until 2026-09-23. A configuration written then keeps working,
    and `doctor` says what to rename."""
    [project] = a_project(
        tmp_path,
        lines=[
            "checks:",
            "  proof: NOTES/checks/proof.md",
            "transitions:",
            "  discovery: {checks: [proof]}",
        ],
    )

    assert project.inspections == {"proof": Path("NOTES/checks/proof.md")}
    assert project.transitions["discovery"].inspections == ("proof",)
    assert project.older == (
        "the project: `checks:` is now `inspections:`",
        "transition 'discovery': `checks:` is now `inspect:`",
    )


def test_the_old_name_and_the_new_one_together_are_refused(tmp_path) -> None:
    """Which of the two was meant is not something to guess."""
    with pytest.raises(ValueError, match="both `inspections:` and `checks:`"):
        a_project(
            tmp_path,
            lines=["checks:", "  proof: NOTES/p.md", "inspections:", "  claims: NOTES/c.md"],
        )


def test_the_names_transitions_had_before_are_still_read_and_said(tmp_path) -> None:
    """`handoffs:` until 2026-09-24, and a step's `handoff:`. A configuration
    written then keeps working, and `doctor` says what to rename."""
    [project] = a_project(
        tmp_path,
        lines=[
            "handoffs:",
            "  review: {to: navigator}",
            "workflows:",
            "  steps:",
            "    reviewing: {handoff: review}",
            "  level1: [reviewing]",
        ],
    )

    assert list(project.transitions) == ["review"]
    assert project.workflows.steps["reviewing"].transition == "review"
    assert project.older == (
        "the project: `handoffs:` is now `transitions:`",
        "step 'reviewing': `handoff:` is now `transition:`",
    )


def test_both_names_for_transitions_at_once_are_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="both `transitions:` and `handoffs:`"):
        a_project(
            tmp_path,
            lines=["handoffs:", "  a: {to: navigator}", "transitions:", "  b: {to: navigator}"],
        )


def test_an_inspection_written_as_a_mapping_still_needs_its_file(tmp_path) -> None:
    """Otherwise it becomes a path spelled with its own braces, and fails only
    when somebody runs it."""
    with pytest.raises(ValueError, match="needs a file"):
        a_project(tmp_path, lines=["inspections:", "  proof: {model: opus}"])


def test_an_inspection_says_nothing_it_does_not_read(tmp_path) -> None:
    """`prompt:` is a transition's. Written here, it would be passed over without a
    word, and the inspection would run on nothing."""
    with pytest.raises(ValueError, match="has prompt — it takes file, model, effort"):
        a_project(tmp_path, lines=["inspections:", "  proof: {prompt: NOTES/proof.md}"])


def test_an_inspection_can_run_on_a_model_of_its_own(tmp_path) -> None:
    """One that needs a stronger model says so where it is described; the rest
    run on the machine's."""
    from halyard.core.config_file import ModelChoice

    [project] = a_project(
        tmp_path,
        lines=[
            "inspections:",
            "  proof: NOTES/inspections/proof.md",
            "  bounded-context:",
            "    file: NOTES/inspections/bounded-context.md",
            "    model: opus",
            "    effort: High",
        ],
    )

    assert project.inspections == {
        "proof": Path("NOTES/inspections/proof.md"),
        "bounded-context": Path("NOTES/inspections/bounded-context.md"),
    }
    assert project.inspection_models == {"bounded-context": ModelChoice("opus", "high")}


def test_what_an_inspection_leaves_unsaid_is_the_machine_s(tmp_path) -> None:
    from halyard.core.config_file import ModelChoice

    [project] = a_project(
        tmp_path, lines=["inspections:", "  proof: {file: NOTES/proof.md, effort: max}"]
    )

    [(name, chosen)] = project.inspection_models.items()
    assert name == "proof"
    assert chosen.over(ModelChoice("sonnet", "high")) == ModelChoice("sonnet", "max")


def test_an_effort_that_is_not_a_name_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="`effort:` must be a name"):
        a_project(tmp_path, lines=["inspections:", "  proof: {file: NOTES/p.md, effort: 3}"])


def test_an_inspection_file_that_is_not_there_is_named_too(tmp_path) -> None:
    from halyard.core.config_file import missing_files

    found = a_project(tmp_path, lines=["inspections:", "  proof: NOTES/GONE.md"])

    [said] = missing_files(found)
    assert "inspections.proof" in said
    assert "NOTES/GONE.md" in said


def test_label_groups_are_read_as_names_and_labels_in_order(tmp_path) -> None:
    """In order, because that is the order a group is searched in."""
    [project] = a_project(
        tmp_path,
        lines=["label_groups:", "  level: [level::1, level::2, level::3]", "  risk: risk::high"],
    )

    assert project.label_groups == {
        "level": ("level::1", "level::2", "level::3"),
        "risk": ("risk::high",),
    }


def test_label_groups_are_empty_unless_written(tmp_path) -> None:
    [project] = a_project(tmp_path, lines=["inspections:", "  proof: NOTES/proof.md"])

    assert project.label_groups == {}


def test_a_label_group_written_as_a_mapping_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="must be a list of labels"):
        a_project(tmp_path, lines=["label_groups:", "  level: {one: level::1}"])


#: A command taking its scope from the task's `ddd-scope` label.
E2E = "  e2e-scope: scripts/halyard-validate.sh test-e2e-scope SCOPE={label_groups.ddd-scope}"


def test_a_command_can_take_a_value_from_a_label_group(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=["label_groups:", "  ddd-scope: [ddd:capstone, ddd:rag]", "commands:", E2E],
    )

    assert project.commands["e2e-scope"].endswith("SCOPE={label_groups.ddd-scope}")


def test_a_command_taking_a_group_nobody_defined_is_refused(tmp_path) -> None:
    """Otherwise the braces would reach the shell as they were written."""
    with pytest.raises(ValueError, match="has no group 'ddd-scope'"):
        a_project(tmp_path, lines=["label_groups:", "  level: [level::3]", "commands:", E2E])


def test_a_command_taking_a_group_with_no_labels_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="lists no labels"):
        a_project(tmp_path, lines=["label_groups:", "  ddd-scope: []", "commands:", E2E])


def test_a_command_can_be_a_list_of_commands_run_in_order(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "commands:",
            "  cleanup: make cleanup",
            "  pull-branch: make pull-branch TASK={input.task}",
            "  next-task: [cleanup, pull-branch]",
        ],
    )

    assert project.command_lists == {"next-task": ("cleanup", "pull-branch")}
    assert set(project.commands) == {"cleanup", "pull-branch"}, "a list is not a line"


def test_a_list_naming_a_command_nobody_defined_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not define: gone"):
        a_project(tmp_path, lines=["commands:", "  cleanup: make x", "  both: [cleanup, gone]"])


def test_a_list_inside_a_list_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="which is a list itself"):
        a_project(
            tmp_path,
            lines=["commands:", "  a: make a", "  inner: [a]", "  outer: [inner, a]"],
        )


def test_an_empty_list_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="empty list"):
        a_project(tmp_path, lines=["commands:", "  nothing: []"])


def test_a_transition_naming_a_list_is_told_to_name_its_commands(tmp_path) -> None:
    with pytest.raises(ValueError, match="which is a list of commands"):
        a_project(
            tmp_path,
            lines=[
                "commands:",
                "  a: make a",
                "  both: [a]",
                "transitions:",
                "  close: {commands: [both]}",
            ],
        )


def test_a_transition_cannot_run_a_command_that_asks_for_a_typed_value(tmp_path) -> None:
    """A transition has nobody to ask."""
    with pytest.raises(ValueError, match="nobody to ask"):
        a_project(
            tmp_path,
            lines=[
                "commands:",
                "  pull: make pull TASK={input.task}",
                "transitions:",
                "  close: {commands: [pull]}",
            ],
        )


def test_validate_cannot_name_a_command_that_takes_a_label(tmp_path) -> None:
    """A commit has nowhere to ask for one."""
    with pytest.raises(ValueError, match="takes a value"):
        a_project(
            tmp_path,
            lines=[
                "label_groups:",
                "  ddd-scope: [ddd:capstone]",
                "commands:",
                E2E,
                "validate: e2e-scope",
            ],
        )


def test_label_findings_are_read_as_phrases(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=["label_findings:", '  - "status: candidate"', '  - "status: evidence missing"'],
    )

    assert project.label_findings == ("status: candidate", "status: evidence missing")


def test_a_finding_phrase_left_unquoted_is_refused_with_why(tmp_path) -> None:
    """`- status: candidate` is a mapping to YAML, and would otherwise become
    text no answer ever contains."""
    with pytest.raises(ValueError, match="has to be quoted"):
        a_project(tmp_path, lines=["label_findings:", "  - status: candidate"])


def test_transitions_are_read_with_what_they_carry(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "inspections:",
            "  proof: NOTES/inspections/proof.md",
            "transitions:",
            "  review: {prompt: NOTES/transitions/review.md, to: reviewer}",
            "  discover_completed: {inspect: [proof], to: navigator}",
        ],
    )

    review = project.transitions["review"]
    assert review.prompt == Path("NOTES/transitions/review.md")
    assert review.include_last_message is True
    assert review.to == "reviewer"
    assert project.transitions["discover_completed"].inspections == ("proof",)


def test_a_transition_can_run_the_projects_commands_first(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "commands:",
            "  test-fast: make test-fast",
            "  lint: make lint",
            "transitions:",
            "  close: {commands: [lint, test-fast], to: navigator}",
        ],
    )

    assert project.transitions["close"].commands == ("lint", "test-fast")


def test_a_transition_naming_a_command_nobody_defined_is_refused(tmp_path) -> None:
    """The same as a check: otherwise it fails only when somebody presses it."""
    with pytest.raises(ValueError, match="does not define: test-all"):
        a_project(tmp_path, lines=["transitions:", "  close: {commands: [test-all]}"])


def test_a_transition_of_commands_alone_is_something_to_hand_on(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "commands:",
            "  test-fast: make test-fast",
            "transitions:",
            "  tests: {commands: [test-fast], include_last_message: false}",
        ],
    )

    assert project.transitions["tests"].include_last_message is False


def test_validate_names_one_of_the_projects_commands(tmp_path) -> None:
    [project] = a_project(
        tmp_path, lines=["commands:", "  test-fast: make test-fast", "validate: test-fast"]
    )

    assert project.validate == "test-fast"


def test_validate_written_as_a_command_line_is_refused(tmp_path) -> None:
    """What Halyard runs for a project is what `commands:` lists, and a line
    written into `validate:` is one that list does not show."""
    written = ["commands:", "  test-fast: make test-fast", "validate: make test-fast"]

    with pytest.raises(ValueError, match="`validate:` names 'make test-fast'"):
        a_project(tmp_path, lines=written)


def test_a_transition_naming_an_inspection_nobody_defined_is_refused(tmp_path) -> None:
    """Otherwise it fails only when somebody presses it, from a phone."""
    with pytest.raises(ValueError, match="does not define: claims"):
        a_project(tmp_path, lines=["transitions:", "  discovery: {inspect: [claims]}"])


def test_a_transition_to_a_seat_that_is_not_there_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="must be a role"):
        a_project(tmp_path, lines=["transitions:", "  review: {to: somebody}"])


def test_a_transition_prompt_that_is_not_there_is_named(tmp_path) -> None:
    from halyard.core.config_file import missing_files

    found = a_project(tmp_path, lines=["transitions:", "  review: {prompt: NOTES/GONE.md}"])

    [said] = missing_files(found)
    assert "transitions.review.prompt" in said


def test_a_transition_can_name_its_text_for_the_rounds_after_the_first(tmp_path) -> None:
    [project] = a_project(
        tmp_path,
        lines=[
            "transitions:",
            "  review:",
            "    prompt: NOTES/transitions/review.md",
            "    followup_prompt: NOTES/transitions/review-followup.md",
            "    to: reviewer",
        ],
    )

    review = project.transitions["review"]
    assert review.prompt == Path("NOTES/transitions/review.md")
    assert review.followup_prompt == Path("NOTES/transitions/review-followup.md")


def with_workflows(tmp_path, *lines: str):
    """A project with two transitions and whatever `workflows:` says here."""
    return a_project(
        tmp_path,
        lines=[
            "transitions:",
            "  review: {prompt: NOTES/transitions/review.md, to: reviewer}",
            "  driver_discover: {prompt: NOTES/transitions/forward.md}",
            "workflows:",
            *lines,
        ],
    )


def test_a_workflow_is_the_steps_it_takes_in_order(tmp_path) -> None:
    [project] = with_workflows(
        tmp_path,
        "  steps:",
        "    review: {seat: reviewer, rounds: 2}",
        "    discover: {transition: driver_discover, seat: reviewer}",
        "  level3: [review, discover, review]",
    )

    flows = project.workflows
    assert flows.flows["level3"] == ("review", "discover", "review")
    assert (flows.steps["review"].transition, flows.steps["review"].rounds) == ("review", 2)
    assert flows.steps["discover"].transition == "driver_discover"
    assert flows.steps["discover"].decided_by is None


def test_a_step_says_nothing_but_its_name_and_is_still_a_step(tmp_path) -> None:
    """The transition is the step's own name, and `to:` says where it goes."""
    [project] = with_workflows(tmp_path, "  steps:", "    review: {}", "  short: [review]")

    step = project.workflows.steps["review"]
    assert (step.transition, step.seat) == ("review", None)
    assert step.rounds == 1, "a step goes once unless it says otherwise"


def test_a_workflow_naming_a_step_nobody_defined_is_refused(tmp_path) -> None:
    """Otherwise it fails part-way through a flow, from a phone."""
    with pytest.raises(ValueError, match="does not define under"):
        with_workflows(tmp_path, "  steps:", "    review: {}", "  level3: [review, gone]")


def test_a_step_naming_a_transition_nobody_defined_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="names the transition 'close'"):
        with_workflows(tmp_path, "  steps:", "    close: {}", "  level3: [close]")


def test_a_step_seat_that_is_not_there_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="must be a role"):
        with_workflows(tmp_path, "  steps:", "    review: {seat: somebody}", "  level3: [review]")


def test_rounds_have_to_be_a_whole_number_of_at_least_one(tmp_path) -> None:
    with pytest.raises(ValueError, match="whole number"):
        with_workflows(tmp_path, "  steps:", "    review: {rounds: 0}", "  level3: [review]")


def test_a_workflow_written_as_anything_but_a_list_is_refused(tmp_path) -> None:
    """Including the mistake of writing `steps:` under a workflow's own name."""
    with pytest.raises(ValueError, match="must be a list of step names"):
        with_workflows(tmp_path, "  steps:", "    review: {}", "  level3: {step1: review}")


def test_a_step_can_act_on_the_decision_of_a_step_named_after_it(tmp_path) -> None:
    """`reviewed` before `review` in the file: the name is checked once every
    step is known."""
    [project] = with_workflows(
        tmp_path,
        "  steps:",
        "    reviewed: {transition: review, decided_by: review}",
        "    review: {}",
        "  level3: [review, reviewed]",
    )

    assert project.workflows.steps["reviewed"].decided_by == "review"


def test_decided_by_naming_no_step_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="must name another step"):
        with_workflows(
            tmp_path,
            "  steps:",
            "    reviewed: {transition: review, decided_by: gone}",
            "  level3: [reviewed]",
        )


def test_a_step_cannot_act_on_its_own_decision(tmp_path) -> None:
    with pytest.raises(ValueError, match="must name another step"):
        with_workflows(tmp_path, "  steps:", "    review: {decided_by: review}", "  l3: [review]")


def test_decisions_are_their_own_names_unless_a_project_renames_them(tmp_path) -> None:
    """Like a key written once standing for its own value: nothing to write
    for `forward`, `back` and `wait`, and any of them can be renamed."""
    from halyard.core.config_file import Decisions

    [project] = with_workflows(
        tmp_path,
        "  decisions: {forward: go, wait: hold}",
        "  steps:",
        "    review: {}",
        "  level3: [review]",
    )

    assert project.workflows.decisions == Decisions(forward="go", back="back", wait="hold")
    assert set(project.workflows.flows) == {"level3"}, "`decisions` is not a workflow"


def test_an_unknown_decision_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown field"):
        with_workflows(tmp_path, "  decisions: {forward: go, sideways: nope}")


def test_two_decisions_cannot_share_a_word(tmp_path) -> None:
    """Otherwise a reply saying it could mean either."""
    with pytest.raises(ValueError, match="cannot share a word"):
        with_workflows(tmp_path, "  decisions: {forward: back}")


def test_a_decision_word_cannot_hold_a_colon(tmp_path) -> None:
    """The word is read after the last colon, so it could never be read."""
    with pytest.raises(ValueError, match="cannot contain"):
        with_workflows(tmp_path, '  decisions: {forward: "go: on"}')


def test_a_list_inside_a_workflow_is_its_phases(tmp_path) -> None:
    """The steps before it go once, the ones after it once, and the ones in it
    once per phase — so the flow is still the steps in order, and the list is
    where they repeat."""
    [project] = with_workflows(
        tmp_path,
        "  steps:",
        "    review: {}",
        "    discover: {transition: driver_discover}",
        "    discovered: {transition: review}",
        "  level3: [review, [discover, discovered], review]",
    )

    assert project.workflows.flows["level3"] == ("review", "discover", "discovered", "review")
    assert project.workflows.stretches == {"level3": (1, 2)}
    assert project.workflows.phases == 3, "three phases unless the project says otherwise"


def test_how_many_phases_is_the_project_s_to_say(tmp_path) -> None:
    [project] = with_workflows(
        tmp_path, "  phases: 4", "  steps:", "    review: {}", "  l3: [[review]]"
    )

    assert project.workflows.phases == 4
    assert project.workflows.stretches == {"l3": (0, 0)}
    assert set(project.workflows.flows) == {"l3"}, "`phases` is not a workflow"


def test_a_workflow_has_one_list_of_phases_at_most(tmp_path) -> None:
    """A second would be a second phase count, and a card could not say which."""
    with pytest.raises(ValueError, match="two lists of phases"):
        with_workflows(tmp_path, "  steps:", "    review: {}", "  l3: [[review], [review]]")


def test_phases_cannot_be_empty_or_nested(tmp_path) -> None:
    for flow in ("[review, []]", "[[review, [review]]]"):
        with pytest.raises(ValueError, match="phases must be a list of step names"):
            with_workflows(tmp_path, "  steps:", "    review: {}", f"  l3: {flow}")


def test_a_phase_step_nobody_defined_is_refused_like_any_other(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not define under"):
        with_workflows(tmp_path, "  steps:", "    review: {}", "  l3: [review, [gone]]")


def test_the_phase_count_has_to_be_a_whole_number_of_at_least_one(tmp_path) -> None:
    with pytest.raises(ValueError, match="phases:` must be a whole number"):
        with_workflows(tmp_path, "  phases: 0", "  steps:", "    review: {}", "  l3: [review]")


def test_next_is_a_decision_word_a_project_can_rename_too(tmp_path) -> None:
    from halyard.core.config_file import Decisions

    [project] = with_workflows(
        tmp_path, "  decisions: {next: sonraki}", "  steps:", "    review: {}", "  l3: [review]"
    )

    assert project.workflows.decisions == Decisions(next="sonraki")


def test_a_project_with_no_workflows_has_none(tmp_path) -> None:
    from halyard.core.config_file import Decisions

    [project] = a_project(tmp_path, lines=["inspections:", "  proof: NOTES/proof.md"])

    assert project.workflows.flows == {}
    assert project.workflows.steps == {}
    assert project.workflows.decisions == Decisions()


def test_a_followup_prompt_that_is_not_there_is_named(tmp_path) -> None:
    from halyard.core.config_file import missing_files

    found = a_project(
        tmp_path, lines=["transitions:", "  review: {followup_prompt: NOTES/GONE.md}"]
    )

    [said] = missing_files(found)
    assert "transitions.review.followup_prompt" in said


def test_a_seat_prompt_file_is_checked_too(tmp_path) -> None:
    from halyard.core.config_file import missing_files, projects_from_yaml

    code = tmp_path / "alpha-engine"
    code.mkdir()
    found = projects_from_yaml(
        f"projects:\n  alpha-engine:\n    path: {code}\n    seats:\n"
        "      nav: {runtime: claude-code, after_compaction: NOTES/orient.md}\n"
    )

    said = missing_files(found)
    assert said and "nav's after_compaction" in said[0]

    (code / "NOTES").mkdir()
    (code / "NOTES" / "orient.md").write_text("here")
    assert missing_files(found) == []


def test_paths_are_read_against_the_project(tmp_path) -> None:
    """Where these files live, and the whole reason the check exists: the ones
    that went missing were relative to somewhere else entirely."""
    from halyard.core.config_file import missing_files

    code = tmp_path / "alpha-engine"
    code.mkdir()
    (tmp_path / "REVIEW.md").write_text("beside the project, not in it")

    found = a_project(tmp_path, lines=["confirmation:", "  review: REVIEW.md"])

    assert missing_files(found), "a file beside the project is not a file in it"


def test_a_project_with_no_path_is_not_guessed_at(tmp_path) -> None:
    """A project can be named and given seats before anybody decides where its
    code lives."""
    from halyard.core.config_file import missing_files, projects_from_yaml

    found = projects_from_yaml(
        "projects:\n  alpha-engine:\n    confirmation: {review: NOTES/GONE.md}\n"
        "    seats:\n      nav: {runtime: claude-code}\n"
    )

    assert missing_files(found) == []


# --- the `runtimes:` block ----------------------------------------------------


def test_a_runtime_can_carry_settings_of_its_own() -> None:
    """Model lists used to live in `settings:` as a comma-separated string,
    which works for one setting and stops working at two."""
    from halyard.core.config_file import runtimes_from_yaml

    found = runtimes_from_yaml(
        """
runtimes:
  opencode:
    port: 4096
    models: [a-model, another-model]
    on_quota: another-model
"""
    )

    assert found["opencode"].port == 4096
    assert found["opencode"].models == ("a-model", "another-model")
    assert found["opencode"].on_quota == "another-model"


def test_no_runtimes_block_is_the_ordinary_case() -> None:
    """Every runtime that predates this works without one."""
    from halyard.core.config_file import runtimes_from_yaml

    assert runtimes_from_yaml("projects: {}") == {}


def test_a_misspelled_runtime_is_refused_rather_than_ignored() -> None:
    """A block that silently applies to nothing is found out by the thing you
    configured not happening."""
    from halyard.core.config_file import runtimes_from_yaml

    with pytest.raises(ValueError, match="is not a runtime"):
        runtimes_from_yaml("runtimes:\n  opencde:\n    port: 4096")


def test_an_unknown_field_is_refused(monkeypatch) -> None:
    from halyard.core.config_file import runtimes_from_yaml

    with pytest.raises(ValueError, match="does not take"):
        runtimes_from_yaml("runtimes:\n  opencode:\n    prt: 4096")


def test_a_port_that_is_not_a_port_is_refused() -> None:
    from halyard.core.config_file import runtimes_from_yaml

    with pytest.raises(ValueError, match="port number"):
        runtimes_from_yaml("runtimes:\n  opencode:\n    port: 99999")


def test_a_fallback_model_has_to_be_one_of_the_offered_ones() -> None:
    """Otherwise the card offers something this configuration never said was
    usable here, and finding out costs a turn."""
    from halyard.core.config_file import runtimes_from_yaml

    with pytest.raises(ValueError, match="not in its `models:` list"):
        runtimes_from_yaml(
            "runtimes:\n  opencode:\n    models: [a-model]\n    on_quota: something-else"
        )


# --- labelling who worked on a task -------------------------------------------

_ONE_SEAT = "    seats: {nav: {runtime: claude-code, session: a}}\n"


def test_label_work_is_off_unless_a_project_asks() -> None:
    """It writes to somebody's issue tracker on its own."""
    [project] = projects_from_yaml("projects:\n  alpha:\n    path: /tmp/alpha\n" + _ONE_SEAT)

    assert project.label_work is False


def test_label_work_can_be_turned_on() -> None:
    [project] = projects_from_yaml(
        "projects:\n  alpha:\n    path: /tmp/alpha\n    label_work: true\n" + _ONE_SEAT
    )

    assert project.label_work is True


def test_label_work_refuses_anything_but_a_boolean() -> None:
    """The generous reading of `"false"` is yes — a string is truthy — and this
    setting writes to an issue tracker."""
    with pytest.raises(ValueError, match="true or false"):
        projects_from_yaml(
            'projects:\n  alpha:\n    path: /tmp/alpha\n    label_work: "false"\n' + _ONE_SEAT
        )


def _seats(*lines: str) -> str:
    return "projects:\n  alpha:\n    path: /tmp/alpha\n    seats:\n" + "".join(
        f"      {line}\n" for line in lines
    )


def test_a_seat_can_name_its_own_task_label() -> None:
    [project] = projects_from_yaml(
        _seats("nav: {runtime: claude-code, role: navigator, task_label: 'navigator:agent'}")
    )

    assert project.seats[0].task_label == "navigator:agent"


def test_a_task_label_with_a_comma_is_refused() -> None:
    """GitLab adds labels as a comma-separated list; one would become two."""
    with pytest.raises(ValueError, match="comma"):
        projects_from_yaml(
            _seats("nav: {runtime: claude-code, role: navigator, task_label: 'a,b'}")
        )


def test_seats_that_cannot_be_told_apart_must_ask_for_the_same_label() -> None:
    """A sighting carries runtime and role and nothing else."""
    with pytest.raises(ValueError, match="tell them apart"):
        projects_from_yaml(
            _seats(
                "nav: {runtime: claude-code, role: navigator, task_label: 'agent:nav'}",
                "nav2: {runtime: claude-code, session: b, role: navigator}",
            )
        )


def test_seats_without_a_role_cannot_ask_for_different_labels_either() -> None:
    with pytest.raises(ValueError, match="without a role"):
        projects_from_yaml(
            _seats(
                "dev: {runtime: claude-code, task_label: developer}", "dev2: {runtime: claude-code}"
            )
        )
