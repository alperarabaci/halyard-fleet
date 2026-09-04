"""Tests for what an agent is never allowed to do.

Two halves. The pattern gets a table, because every entry in it is a real
command somebody's agent will type and the interesting cases are the ones that
must *not* be refused. The gate gets the two properties that make the rule worth
having: it happens before anything else can permit the call, and a pause does
not lift it.
"""

from __future__ import annotations

import pytest

from halyard.core import refusals

# Commands that write history, in the shapes agents actually produce.
REFUSED = [
    ("git commit -m 'fix'", "commit"),
    ("git commit", "commit"),
    ("git push", "push"),
    ("git push --set-upstream origin main", "push"),
    ("git -C /somewhere/else commit -m x", "commit"),
    ("git --no-pager commit --amend", "commit"),
    ("cd packages/api && git commit -am wip", "commit"),
    ("make build; git push", "push"),
    ("sudo git push", "push"),
    ("(git commit)", "commit"),
    ("GIT_TRACE=1 git push\ngit commit", "commit"),
]

# Commands that must survive, each one a way this could have been too eager.
ALLOWED = [
    "git status",
    "git log --oneline -5",
    # A search whose *argument* is the word.
    "git log --grep commit",
    "git show HEAD",
    # Plumbing that makes an object and moves no branch.
    "git commit-tree $tree",
    # An agent writing documentation about committing.
    "echo git commit",
    'echo "then run git commit"',
    "grep -r 'git push' docs/",
    "make test-fast",
    "",
]


@pytest.mark.parametrize(("command", "act"), REFUSED)
def test_a_command_that_writes_history_is_recognised(command: str, act: str) -> None:
    assert refusals.writes_history(command) == act


@pytest.mark.parametrize("command", ALLOWED)
def test_a_command_that_does_not_is_left_alone(command: str) -> None:
    """The expensive mistake here is a false one: an agent refused for running
    `git status` learns nothing and stops working."""
    assert refusals.writes_history(command) is None


def test_the_flag_and_the_pattern_are_read_together() -> None:
    """So a caller cannot apply one without the other."""
    assert refusals.writes_history_if("git commit -m x", False) is None
    assert refusals.writes_history_if("git commit -m x", True) == "commit"
    assert refusals.writes_history_if("git status", True) is None


def test_what_the_agent_is_told_says_what_to_do_instead() -> None:
    """An agent told only "denied" tries the next spelling of the same thing."""
    said = refusals.why("commit")

    assert "Halyard commits" in said
    assert "another way" in said
