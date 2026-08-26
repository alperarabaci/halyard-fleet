"""Tests for how the running log is broken into readable pieces.

One flat file reached thirty-six thousand lines in a month. The unit somebody
actually looks in is the week, so that is the unit it splits on — without
losing the ceiling that keeps an unattended service from filling a disk.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from halyard.logs import WeeklyFileHandler, prune_bridge_logs, week_stamp


def record(message: str = "x") -> logging.LogRecord:
    return logging.LogRecord("halyard", logging.INFO, __file__, 1, message, None, None)


def handler(path: Path, *, keep: int = 8, max_bytes: int = 5_000_000) -> WeeklyFileHandler:
    return WeeklyFileHandler(path, backup_count=keep, max_bytes=max_bytes)


def test_the_week_is_in_the_name(tmp_path: Path) -> None:
    """ISO, so the week number agrees with the year beside it — the days around
    New Year are exactly where a calendar year and an ISO year part company."""
    assert week_stamp(datetime(2026, 8, 26)) == "2026-W35"
    # 1 January 2027 falls in ISO week 53 of 2026.
    assert week_stamp(datetime(2027, 1, 1)) == "2026-W53"


def test_a_full_week_rolls_early_rather_than_filling_the_disk(tmp_path: Path) -> None:
    """Weekly alone would let one bad week — a tight error loop, a runtime
    failing every second — run unbounded until Monday."""
    log = tmp_path / "halyard.log"
    written = handler(log, max_bytes=200)

    for index in range(40):
        written.emit(record(f"line {index} " + "y" * 20))
    written.close()

    rotated = list(tmp_path.glob("halyard.log.*"))
    assert rotated, "the ceiling did nothing"


def test_a_second_roll_in_one_week_keeps_both_halves(tmp_path: Path) -> None:
    """The parent would name both after the same week and delete the first —
    a log quietly eating itself."""
    log = tmp_path / "halyard.log"
    written = handler(log, max_bytes=200)

    for index in range(200):
        written.emit(record(f"line {index} " + "y" * 20))
    written.close()

    rotated = list(tmp_path.glob("halyard.log.*"))
    assert len(rotated) > 1
    assert len({path.name for path in rotated}) == len(rotated), "names collided"


def test_only_the_kept_number_of_files_survives(tmp_path: Path) -> None:
    log = tmp_path / "halyard.log"
    written = handler(log, keep=2, max_bytes=200)

    for index in range(300):
        written.emit(record(f"line {index} " + "y" * 20))
    written.close()

    assert len(list(tmp_path.glob("halyard.log.*"))) <= 2


def test_the_current_week_is_always_the_same_filename(tmp_path: Path) -> None:
    """`tail logs/halyard.log` has to keep working, or every instruction that
    mentions it becomes wrong once a week."""
    log = tmp_path / "halyard.log"
    written = handler(log, max_bytes=200)

    for index in range(40):
        written.emit(record(f"line {index} " + "y" * 20))
    written.close()

    assert log.exists()


# --- the bridge's own weekly files -------------------------------------------


def bridge_log(directory: Path, stamp: str) -> Path:
    path = directory / f"bridge-{stamp}.log"
    path.write_text("a line\n", encoding="utf-8")
    return path


def test_old_bridge_weeks_are_dropped(tmp_path: Path) -> None:
    """The hooks that write these cannot tidy them — several run at once, each
    for under a second. The control plane is the one process here that is alone."""
    for stamp in ("2026-W30", "2026-W31", "2026-W32", "2026-W33"):
        bridge_log(tmp_path, stamp)

    removed = prune_bridge_logs(tmp_path, keep=2)

    assert {path.name for path in removed} == {"bridge-2026-W30.log", "bridge-2026-W31.log"}
    assert (tmp_path / "bridge-2026-W33.log").exists()


def test_pruning_leaves_anything_that_is_not_ours(tmp_path: Path) -> None:
    """A folder somebody also keeps something else in is not ours to clear."""
    bridge_log(tmp_path, "2026-W30")
    bridge_log(tmp_path, "2026-W31")
    keep_me = tmp_path / "bridge-notes.log"
    keep_me.write_text("mine", encoding="utf-8")

    prune_bridge_logs(tmp_path, keep=1)

    assert keep_me.exists()


def test_pruning_nothing_is_not_an_error(tmp_path: Path) -> None:
    assert prune_bridge_logs(tmp_path, keep=2) == []


def test_keeping_none_is_read_as_keep_everything(tmp_path: Path) -> None:
    """`0` is what somebody sets to turn a limit off, and deleting every log
    would be the opposite of what they asked for."""
    bridge_log(tmp_path, "2026-W30")

    assert prune_bridge_logs(tmp_path, keep=0) == []
    assert (tmp_path / "bridge-2026-W30.log").exists()


def test_an_unreadable_directory_is_not_a_reason_to_stop(tmp_path: Path) -> None:
    """Tidying happens on the way to serving. A folder that cannot be listed is
    untidy, not a reason to leave the gate down."""
    assert prune_bridge_logs(tmp_path / "not-there", keep=2) == []
