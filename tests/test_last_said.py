"""Tests for `halyard.core.last_said` — what an agent last said in each chat."""

from __future__ import annotations

import json
from pathlib import Path

from halyard.core import last_said


def test_what_is_kept_says_an_agent_wrote_it(tmp_path: Path) -> None:
    kept = tmp_path / "last-said.json"

    last_said.remember(kept, chat_id="-1004", text="28 files changed.\nDECISION: forward")

    assert json.loads(kept.read_text())["-1004"]["kind"] == last_said.AGENT
    said = last_said.last(kept, "-1004")
    assert said is not None and said.kind == last_said.AGENT


def test_what_halyard_says_in_an_agents_chat_is_not_its_reply(tmp_path: Path) -> None:
    """Measured: a compaction notice took the place of the driver's report, and
    the close handed it on as the report — the inspections judged the notice."""
    kept = tmp_path / "last-said.json"
    last_said.remember(kept, chat_id="-1004", text="28 files changed.\nDECISION: forward")

    last_said.remember(
        kept,
        chat_id="-1004",
        text="✅ <b>xdrv</b> finished compacting in 41s.",
        kind=last_said.HALYARD,
    )

    said = last_said.last(kept, "-1004")
    assert said is not None and said.text == "28 files changed.\nDECISION: forward"


def test_an_entry_that_is_not_an_agents_is_never_handed_on(tmp_path: Path) -> None:
    kept = tmp_path / "last-said.json"
    kept.write_text(
        json.dumps({"-1004": {"text": "a notice", "at": "2026-11-12T10:00:00+00:00", "kind": "x"}})
    )

    assert last_said.last(kept, "-1004") is None


def test_an_entry_kept_before_kinds_were_is_read_as_an_agents(tmp_path: Path) -> None:
    kept = tmp_path / "last-said.json"
    kept.write_text(
        json.dumps({"-1004": {"text": "All 42 passed.", "at": "2026-11-12T10:00:00+00:00"}})
    )

    said = last_said.last(kept, "-1004")
    assert said is not None and said.text == "All 42 passed."
