"""Tests for the tool-name allow list.

Weighted like `test_writes.py`: what must not be granted matters more than what
must, because a false grant hands over a tool nobody meant to hand over.
"""

from __future__ import annotations

import pytest

from halyard.core.tools import NEVER, allowed_by, from_yaml

# --- what it grants ----------------------------------------------------------


def test_a_named_mcp_tool_is_granted() -> None:
    patterns = ("mcp__alpha__list_companies",)

    assert allowed_by("mcp__alpha__list_companies", patterns) == "mcp__alpha__list_companies"


def test_one_pattern_covers_the_same_tool_on_two_servers() -> None:
    """Which is how these are actually deployed — a local server and a
    production one, measured as `mcp__claude_ai_alpha_explore_prod__…`."""
    patterns = ("mcp__*__list_*",)

    assert allowed_by("mcp__alpha_engine__list_companies", patterns)
    assert allowed_by("mcp__claude_ai_alpha_explore_prod__list_companies", patterns)


def test_a_read_pattern_does_not_cover_a_writing_one() -> None:
    """`propose_*` changes something at the other end. Naming `get_*` and
    `list_*` should not quietly include it."""
    patterns = ("mcp__*__list_*", "mcp__*__get_*")

    assert allowed_by("mcp__alpha__propose_prompt", patterns) is None


def test_websearch_can_be_granted_by_name() -> None:
    assert allowed_by("WebSearch", ("WebSearch",)) == "WebSearch"


# --- what it refuses ---------------------------------------------------------


def test_nothing_is_granted_by_default() -> None:
    assert allowed_by("mcp__alpha__list_companies", ()) is None


def test_bash_is_never_granted_however_the_pattern_is_written() -> None:
    """A shell command is what the gate is for. One entry here would hand it
    over wholesale."""
    for pattern in ("Bash", "*", "Ba*", "**"):
        assert allowed_by("Bash", (pattern,)) is None


def test_a_file_tool_is_never_granted_here() -> None:
    """They are granted by destination under `writes:`, which is the narrower
    and more honest question to ask about a write."""
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert allowed_by(tool, ("*",)) is None


def test_an_unnamed_tool_is_refused() -> None:
    assert allowed_by(None, ("*",)) is None


# --- reading the block -------------------------------------------------------


def test_no_block_grants_nothing() -> None:
    assert from_yaml("settings: {}") == ()


def test_the_block_is_read_as_a_list() -> None:
    assert from_yaml("tools:\n  - mcp__*__get_*\n  - WebSearch\n") == ("mcp__*__get_*", "WebSearch")


def test_a_pattern_that_would_reach_bash_is_refused_outright() -> None:
    """Refused when it is written, not silently ignored when it is used — a
    `tools: ["*"]` would otherwise quietly undo the gate."""
    with pytest.raises(ValueError, match="would also grant"):
        from_yaml("tools:\n  - '*'\n")


def test_a_pattern_that_would_reach_a_file_tool_is_refused() -> None:
    with pytest.raises(ValueError, match="would also grant"):
        from_yaml("tools:\n  - 'Writ?'\n")


def test_a_block_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ValueError, match="list of tool-name patterns"):
        from_yaml("tools:\n  mcp: yes\n")


def test_an_empty_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="not empty"):
        from_yaml("tools:\n  - ''\n")


def test_the_never_set_covers_the_shell_and_the_file_tools() -> None:
    assert "Bash" in NEVER and "Write" in NEVER
