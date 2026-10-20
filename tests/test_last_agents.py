"""`last_agents`: the agent each role last worked with, per project."""

from __future__ import annotations

from pathlib import Path

from halyard.core import last_agents


def test_each_role_keeps_its_own_last_agent(tmp_path: Path) -> None:
    kept = tmp_path / "projects" / "alpha-engine" / last_agents.FILE

    last_agents.remember(kept, role="driver", label="drv")
    last_agents.remember(kept, role="navigator", label="xnav")
    last_agents.remember(kept, role="driver", label="xdrv")

    assert last_agents.recent(kept) == {"driver": "xdrv", "navigator": "xnav"}


def test_nothing_kept_or_unreadable_is_nothing(tmp_path: Path) -> None:
    broken = tmp_path / last_agents.FILE
    broken.write_text("{not json", encoding="utf-8")

    assert last_agents.recent(tmp_path / "missing.json") == {}
    assert last_agents.recent(broken) == {}
    assert last_agents.recent(None) == {}


def test_the_same_agent_again_leaves_the_file_alone(tmp_path: Path) -> None:
    """Every reply comes through; the file changes only when the agent does."""
    kept = tmp_path / last_agents.FILE
    last_agents.remember(kept, role="driver", label="xdrv")
    kept.write_text('{"driver": "xdrv", "untouched": "yes"}', encoding="utf-8")

    last_agents.remember(kept, role="driver", label="xdrv")

    assert "untouched" in kept.read_text(encoding="utf-8")
