"""Tests for `halyard.transitions.rounds` — which work a round belongs to, and how
one is shown. The rounds themselves are a workflow's: see `test_workflows.py`."""

from __future__ import annotations

from halyard.transitions import rounds


def test_a_numbered_branch_counts_against_its_work_item() -> None:
    assert rounds.work_of("361-fix-n8n-review-bugs", "alpha-engine") == "alpha-engine#361"


def test_a_branch_without_a_number_counts_against_its_own_name() -> None:
    assert rounds.work_of("try-the-new-loader", "alpha-engine") == "try-the-new-loader"


def test_a_detached_head_counts_nothing() -> None:
    assert rounds.work_of(None, "alpha-engine") is None


def test_a_round_is_shown_against_what_its_step_allows() -> None:
    assert rounds.shown(2, 2) == "2/2"
    assert rounds.shown(3, 2) == "3/2", "past it, it still says so"
    assert rounds.shown(2) == "2", "and without a number, nothing is invented"
