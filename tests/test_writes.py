"""Tests for the one place Halyard grants without asking.

Weighted deliberately: the cases that must *not* grant outnumber the ones that
must, because a false grant hands an agent write access somebody did not intend
and a false refusal costs one tap on a phone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard.core.writes import FILE_TOOLS, allowed_by


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "NOTES" / "development-prompts").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    return tmp_path


# --- what it grants ----------------------------------------------------------


def test_a_file_under_a_named_directory_is_granted(project: Path) -> None:
    assert allowed_by(str(project / "NOTES" / "a.md"), str(project), ("NOTES/**",)) == "NOTES/**"


def test_a_nested_file_is_granted_by_a_crossing_glob(project: Path) -> None:
    target = project / "NOTES" / "development-prompts" / "p2.md"

    assert allowed_by(str(target), str(project), ("NOTES/**",))


def test_a_bare_directory_name_means_everything_under_it(project: Path) -> None:
    """What somebody writing `NOTES` rather than `NOTES/**` has in mind."""
    target = project / "NOTES" / "development-prompts" / "p2.md"

    assert allowed_by(str(target), str(project), ("NOTES",))


def test_a_relative_path_is_measured_from_the_project(project: Path) -> None:
    """Never from wherever the control plane happens to be running: its working
    directory is a fact about the service, not about the write."""
    assert allowed_by("NOTES/a.md", str(project), ("NOTES/**",)) == "NOTES/**"


def test_a_relative_path_cannot_climb_out_either(project: Path) -> None:
    assert allowed_by("NOTES/../../etc/passwd", str(project), ("NOTES/**",)) is None


def test_an_extension_glob_grants_only_that_extension(project: Path) -> None:
    patterns = ("NOTES/*.md",)

    assert allowed_by(str(project / "NOTES" / "a.md"), str(project), patterns)
    assert allowed_by(str(project / "NOTES" / "a.py"), str(project), patterns) is None


# --- what it refuses ---------------------------------------------------------


def test_nothing_is_granted_by_default(project: Path) -> None:
    """The default is empty. Nothing is pre-authorized by forgetting to set it."""
    assert allowed_by(str(project / "NOTES" / "a.md"), str(project), ()) is None


def test_climbing_out_of_the_project_is_refused(project: Path) -> None:
    """`NOTES/../../../etc/passwd` starts with `NOTES/` and is not in NOTES.

    This is the traversal the whole module is arranged around: a grant that
    matched on the spelling rather than the destination would be wide open.
    """
    escape = str(project / "NOTES" / ".." / ".." / "etc" / "passwd")

    assert allowed_by(escape, str(project), ("NOTES/**",)) is None


def test_an_absolute_path_elsewhere_is_refused(project: Path) -> None:
    assert allowed_by("/etc/passwd", str(project), ("NOTES/**", "**")) is None


def test_a_symlink_leading_out_of_the_project_is_refused(project: Path, tmp_path: Path) -> None:
    """Resolved before it is compared, so a link out of the tree and a `..` out
    of it are the same refusal."""
    outside = tmp_path.parent / "outside-the-project"
    outside.mkdir(exist_ok=True)
    (project / "NOTES" / "escape").symlink_to(outside, target_is_directory=True)

    escaped = str(project / "NOTES" / "escape" / "x.md")

    assert allowed_by(escaped, str(project), ("NOTES/**",)) is None


def test_a_single_star_does_not_cross_directories(project: Path) -> None:
    """A `*` that quietly spanned directories would grant a subtree somebody
    thought they had excluded."""
    nested = project / "NOTES" / "development-prompts" / "p2.md"

    assert allowed_by(nested, str(project), ("NOTES/*",)) is None


def test_a_sibling_directory_sharing_a_prefix_is_refused(project: Path) -> None:
    """`NOTES-private` is not `NOTES`, however similar it looks."""
    (project / "NOTES-private").mkdir()
    target = project / "NOTES-private" / "secret.md"

    assert allowed_by(str(target), str(project), ("NOTES/**",)) is None
    assert allowed_by(str(target), str(project), ("NOTES",)) is None


def test_no_project_means_no_grant(project: Path) -> None:
    """With nothing to measure the path against, a pattern cannot be scoped —
    so it does not apply at all."""
    assert allowed_by(str(project / "NOTES" / "a.md"), None, ("NOTES/**",)) is None


def test_no_path_means_no_grant(project: Path) -> None:
    assert allowed_by(None, str(project), ("NOTES/**",)) is None


def test_an_empty_pattern_grants_nothing(project: Path) -> None:
    """A blank line in the configuration must not become `everything`."""
    assert allowed_by(str(project / "src" / "main.py"), str(project), ("", "   ", "/")) is None


# --- the shape of the thing --------------------------------------------------


def test_the_file_tools_are_the_ones_that_take_a_path() -> None:
    assert "Write" in FILE_TOOLS and "Edit" in FILE_TOOLS
    # Bash is gated too, but it is not a file tool and is never pre-authorized
    # by a path — its whole argument is a command, not a destination.
    assert "Bash" not in FILE_TOOLS


# --- reading the block out of halyard.yaml ----------------------------------


def test_no_block_grants_nothing() -> None:
    from halyard.core.writes import from_yaml

    assert from_yaml("settings: {}") == ()


def test_the_block_is_read_as_a_list_of_patterns() -> None:
    from halyard.core.writes import from_yaml

    assert from_yaml("writes:\n  - NOTES/**\n  - docs/**\n") == ("NOTES/**", "docs/**")


def test_a_block_that_is_not_a_list_is_refused() -> None:
    """A grant somebody believes they have written and that is silently absent
    is the worse of the two failures — they would leave the desk expecting it."""
    from halyard.core.writes import from_yaml

    with pytest.raises(ValueError, match="list of path patterns"):
        from_yaml("writes:\n  NOTES: yes\n")


def test_an_empty_entry_is_refused() -> None:
    from halyard.core.writes import from_yaml

    with pytest.raises(ValueError, match="not empty"):
        from_yaml("writes:\n  - ''\n")


def test_an_absolute_pattern_is_refused() -> None:
    """It reads as though it grants that path anywhere, and it never can —
    patterns are matched inside the project the write belongs to."""
    from halyard.core.writes import from_yaml

    with pytest.raises(ValueError, match="absolute path"):
        from_yaml("writes:\n  - /etc/**\n")
