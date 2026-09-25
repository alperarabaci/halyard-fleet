"""Tests for `halyard.commands.inputs` — a command line taking a value somebody
types."""

from __future__ import annotations

from halyard.commands.inputs import filled, names_in


def test_a_line_names_the_values_it_asks_for_in_order_each_once() -> None:
    line = "make pull-branch TASK={input.task} FROM={input.base} AGAIN={input.task}"

    assert names_in(line) == ("task", "base")


def test_a_label_group_or_a_shell_brace_is_not_a_typed_value() -> None:
    line = "make e2e SCOPE={label_groups.ddd-scope} HOME=${HOME} {task}"

    assert names_in(line) == ()
    assert filled(line, {"task": "369"}) == line


def test_what_was_typed_arrives_as_one_word() -> None:
    assert filled("make pull-branch TASK={input.task}", {"task": "369"}) == (
        "make pull-branch TASK=369"
    )
    assert filled("echo {input.task}", {"task": "7; rm -rf ."}) == "echo '7; rm -rf .'"


def test_a_value_not_given_is_left_as_written() -> None:
    assert filled("make pull-branch TASK={input.task}", {}) == "make pull-branch TASK={input.task}"
