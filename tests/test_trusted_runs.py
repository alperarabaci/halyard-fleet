"""Tests for the commands each project trusts, kept in the database."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from halyard.core import trusted_runs


def test_an_entry_is_kept_and_read_back(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"

    assert trusted_runs.add(database, "alpha-engine", "uv run pytest *", by="test")

    assert trusted_runs.entries(database) == {"alpha-engine": ("uv run pytest *",)}
    assert trusted_runs.entries(database, "other") == {}


def test_the_same_entry_twice_is_kept_once(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"

    assert trusted_runs.add(database, "a", "make test", by="test")
    assert not trusted_runs.add(database, "a", " make test ", by="test")

    assert trusted_runs.entries(database) == {"a": ("make test",)}


def test_an_entry_that_could_run_anything_is_never_kept(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"

    with pytest.raises(ValueError, match="could run anything"):
        trusted_runs.add(database, "a", "bash *", by="test")

    assert trusted_runs.entries(database) == {}


def test_one_kept_under_older_rules_is_skipped_when_read_back(tmp_path: Path) -> None:
    """Rules can only tighten. An entry they no longer take is not trusted
    because it was once written down."""
    database = tmp_path / "halyard.db"
    trusted_runs.add(database, "a", "make test", by="test")
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO trusted_runs VALUES ('a', 'bash *', '2026-09-25', 'older')")
        db.commit()

    assert trusted_runs.entries(database) == {"a": ("make test",)}


def test_an_entry_can_be_removed(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    trusted_runs.add(database, "a", "make test", by="test")

    assert trusted_runs.remove(database, "a", "make test")
    assert not trusted_runs.remove(database, "a", "make test")
    assert trusted_runs.entries(database) == {}


def test_nothing_kept_reads_as_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "halyard.db"

    assert trusted_runs.entries(missing) == {}
    assert not missing.exists(), "reading made no database"


def test_an_export_reads_back_as_itself() -> None:
    text = trusted_runs.export_text({"b": ["make test"], "a": ["uv run pytest *"], "none": []})

    assert trusted_runs.import_text(text) == {"a": ["uv run pytest *"], "b": ["make test"]}


@pytest.mark.parametrize(
    "text",
    ["runs: [make test]", "writes:\n  - x\n", "runs:\n  a: make test\n", "runs: {a: [1]}", ":"],
)
def test_a_file_of_another_shape_is_refused_whole(text: str) -> None:
    """Half of a list read is a list nobody wrote."""
    with pytest.raises(ValueError):
        trusted_runs.import_text(text)
