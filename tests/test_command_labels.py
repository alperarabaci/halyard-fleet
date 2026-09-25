"""Tests for `halyard.commands.labels` — a command line taking a value from the
task's labels, from a group the project wrote down."""

from __future__ import annotations

from halyard.commands.labels import filled, groups_in, missing, value_of, values_from

LINE = "scripts/halyard-validate.sh test-e2e-scope SCOPE={label_groups.ddd-scope}"
GROUPS = {"ddd-scope": ("ddd:capstone", "ddd:refining", "ddd:power_generation", "ddd:all")}


def test_a_line_names_the_groups_it_takes_values_from() -> None:
    line = "make e2e SCOPE={label_groups.ddd-scope} LEVEL={label_groups.level} {label_groups.level}"

    assert groups_in(line) == ("ddd-scope", "level")


def test_a_brace_of_the_shell_s_own_is_not_a_group() -> None:
    """`${HOME}`, `find -exec {}` and `{a,b}` are the shell's."""
    line = "find ${HOME} -name '*.log' -exec rm {} \\; && echo {a,b} {scope}"

    assert groups_in(line) == ()
    assert filled(line, {"scope": "capstone"}) == line


def test_a_label_gives_what_follows_its_last_colon() -> None:
    assert value_of("ddd:capstone") == "capstone"
    assert value_of("level::3") == "3"
    assert value_of("rag") == "rag"


def test_every_label_the_task_carries_from_the_group_in_the_groups_order() -> None:
    """Spelled as the group spells it, whatever case the task used."""
    carried = ("backend", "DDD:Refining", "ddd:capstone")

    assert values_from(GROUPS, carried) == {"ddd-scope": "capstone,refining"}


def test_a_group_the_task_carries_nothing_from_has_no_value() -> None:
    assert values_from(GROUPS, ("backend", "level::3")) == {}
    assert missing(LINE, {}) == ("ddd-scope",)


def test_the_value_goes_into_the_line_quoted_for_the_shell() -> None:
    assert filled(LINE, {"ddd-scope": "capstone,refining"}) == (
        "scripts/halyard-validate.sh test-e2e-scope SCOPE=capstone,refining"
    )
    assert filled("run {label_groups.area}", {"area": "two words"}) == "run 'two words'"


def test_a_group_with_no_value_is_left_as_written() -> None:
    assert filled(LINE, {}) == LINE
