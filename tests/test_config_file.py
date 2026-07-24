"""The YAML configuration: projects, and the seats sitting in them.

The shared contract in `test_seat_contract.py` already proves this producer
agrees with the environment one about what a seat is. What is here is what YAML
can express and the environment cannot — a project, its path, and more than one
of them — plus the precedence rule between the two files.
"""

from __future__ import annotations

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
