"""Tests for the Codex usage-window watcher.

The property that matters most is not the phrasing — it is that a batch of
lines produces *one* reading. Codex writes its accounting on every turn, so a
poll that catches up on twenty turns holds twenty readings of the same window,
nineteen of them stale. Getting that wrong sends a burst of contradictory
messages to a phone, which is worse than sending none.

After that: said once, said again when the window rolls over, and never raised
into the poller that feeds it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from halyard.agents.codex.watching import WATCHING, _window_name, alerts, transcript

#: Two timestamps that are not the same window. Fixed rather than computed, so
#: the reset wording is checked against a value and not against itself.
RESETS = 1787832639
LATER = RESETS + 18_000


@pytest.fixture(autouse=True)
def _one_timezone() -> None:
    """A reset time is rendered in the machine's own zone, which is right — the
    machine belongs to whoever reads the message — and would otherwise make
    this file assert something different on every machine. Measured: written
    here it passed, and under CI's UTC five of these failed.

    Pinned rather than computed from the same call the code makes, because a
    test that derives its expectation that way checks nothing.
    """
    before = time.tzname
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "Europe/Istanbul")
        time.tzset()
        yield
    time.tzset()
    assert time.tzname == before


def reading(primary: float | None = None, secondary: float | None = None, resets=RESETS) -> str:
    """One `token_count` line, as Codex writes it."""
    limits: dict = {"plan_type": "plus"}
    if primary is not None:
        limits["primary"] = {
            "used_percent": primary,
            "window_minutes": 300,
            "resets_at": resets,
        }
    if secondary is not None:
        limits["secondary"] = {
            "used_percent": secondary,
            "window_minutes": 10080,
            "resets_at": resets,
        }
    return json.dumps({"type": "event_msg", "payload": {"rate_limits": limits}})


def texts(lines: list[str], seen: set[str] | None = None) -> list[str]:
    return [a.text for a in alerts(lines, seen if seen is not None else set())]


def test_a_window_that_is_filling_is_reported_with_its_percentage() -> None:
    assert texts([reading(primary=91.0)]) == ["is at 91% of its 5h Codex limit, resets 15:10."]


def test_a_window_below_the_line_says_nothing() -> None:
    """The common case. A watcher that speaks on every turn gets muted."""
    assert texts([reading(primary=45.0, secondary=12.0)]) == []


def test_a_full_window_says_so_rather_than_its_percentage() -> None:
    """The higher threshold wins, so a window that jumped past both says the
    thing that explains why work stopped."""
    assert texts([reading(primary=100.0)]) == ["has used its whole 5h Codex limit, resets 15:10."]


def test_both_windows_are_reported_and_named_by_their_length() -> None:
    """`primary` and `secondary` say nothing on a phone; "5h" and "weekly" do."""
    assert texts([reading(primary=93.0, secondary=100.0)]) == [
        "is at 93% of its 5h Codex limit, resets 15:10.",
        "has used its whole weekly Codex limit, resets 15:10.",
    ]


def test_only_the_last_reading_in_a_batch_is_used() -> None:
    """Twenty turns of catching up is still one true number.

    This is the whole reason the reading is taken at the end rather than per
    line: the earlier ones are history, and history is not worth a message.
    """
    catching_up = [reading(primary=p) for p in (91.0, 94.0, 97.0, 100.0)]
    assert texts(catching_up) == ["has used its whole 5h Codex limit, resets 15:10."]


def test_what_was_said_once_is_not_said_again() -> None:
    seen: set[str] = set()
    first = alerts([reading(primary=91.0)], seen)
    seen.update(a.key for a in first)

    assert [a.text for a in first]
    assert alerts([reading(primary=93.0)], seen) == []


def test_a_window_that_has_reset_is_a_new_fact() -> None:
    """The key carries the reset time, so the next window is reported even
    though the threshold and the window are the same."""
    seen = {a.key for a in alerts([reading(primary=91.0)], set())}
    assert texts([reading(primary=91.0, resets=LATER)], seen)


def test_filling_and_then_full_are_two_separate_messages() -> None:
    """Both thresholds are worth saying, in order, within one window."""
    seen = {a.key for a in alerts([reading(primary=91.0)], set())}
    assert texts([reading(primary=100.0)], seen) == [
        "has used its whole 5h Codex limit, resets 15:10."
    ]


def test_lines_that_are_not_readings_are_skipped_not_raised() -> None:
    """It is fed a live file mid-write. Anything in there must not reach the
    poller as an exception."""
    junk = [
        "",
        "   ",
        "not json at all",
        "[1, 2, 3]",
        '"a bare string"',
        json.dumps({"payload": None}),
        json.dumps({"payload": {"rate_limits": "not a dict"}}),
        json.dumps({"payload": {"rate_limits": {"primary": "not a dict"}}}),
        json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": None}}}}),
    ]
    assert alerts(junk, set()) == []
    assert texts([*junk, reading(primary=100.0)])


def test_a_reset_time_the_runtime_did_not_give_is_left_out() -> None:
    """Rather than rendering an epoch, or a wrong time."""
    assert texts([reading(primary=100.0, resets=None)]) == ["has used its whole 5h Codex limit."]


def test_a_window_length_is_rendered_the_way_somebody_would_say_it() -> None:
    assert _window_name(300) == "5h"
    assert _window_name(10080) == "weekly"
    assert _window_name(1440) == "1-day"
    assert _window_name(90) == "90m"
    assert _window_name(None) == "usage"
    assert _window_name(0) == "usage"


def test_a_rollout_is_found_by_the_session_id_in_its_name(tmp_path: Path) -> None:
    """Codex names the file `rollout-<timestamp>-<id>.jsonl` under a dated
    directory, so the id is a suffix — which is why this is the runtime's job
    and not something core could have guessed."""
    day = tmp_path / "sessions" / "2026" / "05" / "24"
    day.mkdir(parents=True)
    wanted = day / "rollout-2026-05-24T20-23-06-019e5b03-0ab3-7690.jsonl"
    wanted.write_text(reading(primary=91.0))
    (day / "rollout-2026-05-24T09-00-00-someone-else.jsonl").write_text("{}")

    assert transcript("019e5b03-0ab3-7690", tmp_path) == wanted


def test_an_id_with_no_rollout_finds_nothing(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    assert transcript("nothing-here", tmp_path) is None


def test_a_missing_root_is_skipped_rather_than_raised(tmp_path: Path) -> None:
    assert transcript("anything", tmp_path / "not-there") is None


def test_the_registry_gets_a_watcher_pointed_at_codexs_own_home() -> None:
    """The spec is what core reads; a watcher core cannot reach is not wired."""
    assert WATCHING.home.name == ".codex"
    assert WATCHING.alerts is alerts
    assert WATCHING.transcript is transcript
