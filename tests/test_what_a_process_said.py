"""Tests for what is kept of a failing CLI's output.

The end of it, and the reason is a real message that reached a phone with
every word of it true and none of it the answer:

    OpenAI Codex v0.145.0
    workdir: …/alpha-engine
    model: gpt-5.6-terra
    provider: openai
    …

The limit had been reached and would reset at 4:26 PM. That sentence was in
the output, past the four-hundred-character cut taken from the front.
"""

from __future__ import annotations

from halyard.core.said_by_a_process import the_useful_end

#: What Codex prints before it prints a problem, as measured.
BANNER = """\
OpenAI Codex v0.145.0
--------
workdir: /Users/someone/alpha-engine
model: gpt-5.6-terra
provider: openai
approval: never
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
reasoning effort: high
reasoning summaries: none
session id: 019e5b03-0ab3-7690-85e0-7
"""


def test_the_reason_survives_the_banner_in_front_of_it() -> None:
    """The failure this exists for. Taken from the front, the banner was the
    whole budget and the sentence underneath it was never sent."""
    said = the_useful_end(BANNER + "\nYou have hit your usage limit. Resets at 4:26 PM.")

    assert "usage limit" in said
    assert "Resets at 4:26 PM" in said


def test_a_traceback_keeps_the_part_that_names_the_error() -> None:
    """Which is also at the end, and is the reason six lines rather than two."""
    said = the_useful_end(
        "Traceback (most recent call last):\n"
        '  File "a.py", line 3, in <module>\n'
        "    boom()\n"
        '  File "a.py", line 1, in boom\n'
        '    raise ValueError("no such session")\n'
        "ValueError: no such session"
    )

    assert said.endswith("ValueError: no such session")


def test_a_one_line_error_is_left_exactly_as_it_is() -> None:
    """The common case, and it must not be padded or trimmed."""
    assert the_useful_end("Not logged in · Please run /login") == (
        "Not logged in · Please run /login"
    )


def test_blank_lines_are_not_spent_on_the_budget() -> None:
    """A CLI that separates its banner from its message with two newlines would
    otherwise use two of the six lines saying nothing."""
    said = the_useful_end("a\n\n\nb\n\n\nc\n\n\nd\n\n\ne\n\n\nf\n\n\ng")

    assert said.splitlines() == ["b", "c", "d", "e", "f", "g"]


def test_a_single_enormous_line_is_cut_from_the_end() -> None:
    """One line can be a whole JSON document. Cutting it from the front would
    reintroduce the same fault: the end is the part worth having."""
    said = the_useful_end("x" * 5000 + "THE ACTUAL PROBLEM")

    assert said.endswith("THE ACTUAL PROBLEM")
    assert len(said) <= 600


def test_nothing_said_is_nothing_returned() -> None:
    """So the caller can tell "it printed nothing" from "it printed this",
    which are different things to tell somebody."""
    assert the_useful_end(None) == ""
    assert the_useful_end("") == ""
    assert the_useful_end("   \n\n  ") == ""
