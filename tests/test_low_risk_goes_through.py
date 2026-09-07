"""Tests for the third grant: a command the rules call low.

Two weeks of real use produced 1406 shell approvals from one runtime, and the
great majority were a `grep`, an `ls` or a test run. A phone asked three
hundred times a day is a phone somebody stops reading, and the cards that
matter arrive in that noise. So the rules that already classify a command are
allowed to end the question.

What carries this is `policy.classify` taking the **highest** risk of
everything it matches. A command is low here only when nothing in it is
anything else — which is why a compound command is not a special case and does
not need to be.
"""

from __future__ import annotations

import pytest

from halyard.config import Settings
from halyard.core.events import RiskLevel
from halyard.core.policy import Policy, without_a_leading_cd


def risk(command: str) -> tuple[RiskLevel, bool]:
    decided = Policy().classify(command)
    return decided.risk, decided.defaulted


# --- the prefix that hid a fortnight of greps ---------------------------------


def test_a_cd_into_the_project_is_not_the_command() -> None:
    """Measured: 1005 of 1406 shell approvals began `cd <project> && …`, and
    every rule that would have called the rest low is anchored at the start of
    the line. The prefix hid it."""
    assert risk("cd /a/project && grep -rn thing .") == (RiskLevel.LOW, False)


def test_stripping_the_prefix_reveals_risk_it_never_hid() -> None:
    """The whole reason this is a strip and not an extra low-risk rule. Adding
    `cd` to the read rules was the shorter fix: it would have called
    `cd /project && python3 whatever.py` low, because `cd` matched and nothing
    else did."""
    assert risk("cd /a/project && python3 whatever.py") == (RiskLevel.MEDIUM, True)
    assert risk("cd /a/project && rm -rf build") == (RiskLevel.HIGH, False)
    assert risk("cd /a/project && cat >> notes.py") == (RiskLevel.MEDIUM, False)


@pytest.mark.parametrize(
    "command",
    [
        'cd "/a b" && rm -rf /',  # a quoted path is not stripped
        "cd /a; rm -rf /",  # nor is a `;`
        "cd /a && grep x && rm -rf /",  # nor does one strip reach past the first
        "cd $(evil) && grep x",  # nor a substitution
        "cd /a | rm -rf /",
    ],
)
def test_nothing_dangerous_survives_the_strip(command: str) -> None:
    """What is removed has to be provably inert, because everything after it is
    what gets judged. If a `;` could hide in the prefix, so could a command."""
    assert without_a_leading_cd(command) == command or risk(command)[0] == RiskLevel.HIGH


def test_a_command_that_is_only_a_cd_is_left_alone() -> None:
    assert without_a_leading_cd("cd /a/project") == "cd /a/project"


# --- what may be skipped ------------------------------------------------------


def test_only_low_may_be_configured() -> None:
    """Medium covers `git commit`, `mv` and `docker compose up`. A setting that
    could be turned up to medium would be a switch for disabling the gate,
    written to look like a preference."""
    assert (
        Settings(
            HALYARD_CHANNEL="stub_allow",
            HALYARD_ALLOW_RISK_AT_OR_BELOW="low",
            _env_file=None,
        ).allow_risk_at_or_below
        == "low"
    )
    with pytest.raises(ValueError, match="takes `low`"):
        Settings(
            HALYARD_CHANNEL="stub_allow",
            HALYARD_ALLOW_RISK_AT_OR_BELOW="medium",
            _env_file=None,
        )


def test_it_is_off_unless_somebody_asks() -> None:
    """Nothing changes for anybody who has not turned it on."""
    assert Settings(HALYARD_CHANNEL="stub_allow", _env_file=None).allow_risk_at_or_below is None


@pytest.mark.parametrize(
    ("command", "skipped"),
    [
        ("cd /a && grep -rn x .", True),
        ("cd /a && pytest -q", True),
        ("git status", True),
        ("ruff check .", True),
        ("docker ps", True),
        ("git commit -m x", False),
        ("mv a b", False),
        ("cd /a && python3 x.py", False),
        ("cd /a && rm -rf b", False),
        ("sudo anything", False),
        ("terraform apply", False),
    ],
)
def test_which_commands_stop_being_questions(command: str, skipped: bool) -> None:
    level, defaulted = risk(command)
    assert ((not defaulted) and level is RiskLevel.LOW) is skipped


def test_an_unrecognised_command_is_still_a_question() -> None:
    """Nothing matching is not a quiet kind of low. It is a command no rule has
    an opinion about, and those are the ones worth a person."""
    level, defaulted = risk("some-tool-nobody-wrote-a-rule-for --go")

    assert defaulted
    assert level is RiskLevel.MEDIUM
