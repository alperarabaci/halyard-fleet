"""Tests for `halyard.handoffs.rounds` — how many times a handoff has gone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.handoffs import rounds

EARLY = datetime(2026, 9, 15, 14, 7, tzinfo=UTC)
LATER = EARLY + timedelta(minutes=10)


def test_a_numbered_branch_counts_against_its_work_item() -> None:
    assert rounds.work_of("361-fix-n8n-review-bugs", "alpha-engine") == "alpha-engine#361"


def test_a_branch_without_a_number_counts_against_its_own_name() -> None:
    assert rounds.work_of("try-the-new-loader", "alpha-engine") == "try-the-new-loader"


def test_a_detached_head_counts_nothing() -> None:
    assert rounds.work_of(None, "alpha-engine") is None


def test_rounds_are_kept_per_work_item_and_per_handoff(tmp_path: Path) -> None:
    kept = tmp_path / "projects" / "alpha-engine" / "handoff-rounds.json"

    assert rounds.record(kept, "alpha-engine#361", "review", to="xrev", now=EARLY) == 1
    assert rounds.record(kept, "alpha-engine#361", "review", to="xrev", now=LATER) == 2
    rounds.record(kept, "alpha-engine#361", "driver_discover", to="drv", now=EARLY)
    rounds.record(kept, "alpha-engine#362", "review", to="xrev", now=EARLY)

    review = rounds.taken(kept, "alpha-engine#361", "review")
    assert [taken.at for taken in review] == [EARLY, LATER]
    assert {taken.to for taken in review} == {"xrev"}
    assert len(rounds.taken(kept, "alpha-engine#361", "driver_discover")) == 1
    assert len(rounds.taken(kept, "alpha-engine#362", "review")) == 1


def test_nothing_counted_yet_is_no_rounds(tmp_path: Path) -> None:
    assert rounds.taken(tmp_path / "handoff-rounds.json", "alpha-engine#361", "review") == []


def test_a_file_that_cannot_be_read_counts_from_nothing(tmp_path: Path) -> None:
    kept = tmp_path / "handoff-rounds.json"
    kept.write_text("{not json")

    assert rounds.taken(kept, "alpha-engine#361", "review") == []
    assert rounds.record(kept, "alpha-engine#361", "review", to="xrev") == 1
    assert len(rounds.taken(kept, "alpha-engine#361", "review")) == 1


def test_the_work_with_the_oldest_round_is_forgotten_first(tmp_path: Path, monkeypatch) -> None:
    kept = tmp_path / "handoff-rounds.json"
    monkeypatch.setattr(rounds, "WORKS", 2)

    rounds.record(kept, "alpha-engine#1", "review", to="xrev", now=EARLY)
    rounds.record(kept, "alpha-engine#2", "review", to="xrev", now=LATER)
    rounds.record(kept, "alpha-engine#3", "review", to="xrev", now=LATER + timedelta(hours=1))

    assert rounds.taken(kept, "alpha-engine#1", "review") == []
    assert len(rounds.taken(kept, "alpha-engine#2", "review")) == 1
    assert len(rounds.taken(kept, "alpha-engine#3", "review")) == 1


def test_a_round_is_shown_against_the_two_expected() -> None:
    assert rounds.shown(1) == "1/2"
    assert rounds.shown(3) == "3/2"
