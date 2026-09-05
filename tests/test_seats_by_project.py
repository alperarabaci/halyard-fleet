"""Tests for finding a seat by the codebase it works in.

Every other way of finding a seat starts from a name the runtime knows the
session by. One runtime has no such name — an id nobody types, and a title it
writes from the content of the conversation and rewrites as that content
changes. A seat pinned to either comes loose.

What this must not do is guess. Routing a reply to the wrong group is the
failure `for_session` was rewritten to prevent, and it is silent when it
happens: the message arrives, it is just in somebody else's chat.
"""

from __future__ import annotations

from halyard.core.events import Role
from halyard.core.seats import Seat, for_project


def seat(label: str, runtime: str = "opencode", **rest) -> Seat:
    return Seat(label=label, runtime=runtime, **rest)


def test_one_seat_of_that_runtime_in_that_project_is_the_answer() -> None:
    seats = [seat("onav", project="alpha-engine", chat="-100")]

    assert for_project(seats, "opencode", "alpha-engine") is seats[0]


def test_a_seat_in_another_project_is_not_it() -> None:
    """The whole point of matching on the project rather than the runtime."""
    seats = [seat("onav", project="something-else")]

    assert for_project(seats, "opencode", "alpha-engine") is None


def test_another_runtime_in_the_same_project_is_not_it() -> None:
    """A project usually has seats for several runtimes at once — this one has
    six. Matching the project alone would hand opencode's card to whichever was
    written first, which is how a Codex reply once reached a Claude group."""
    seats = [
        seat("nav", runtime="claude-code", project="alpha-engine"),
        seat("xnav", runtime="codex", project="alpha-engine"),
        seat("onav", project="alpha-engine"),
    ]

    assert for_project(seats, "opencode", "alpha-engine") is seats[2]


def test_two_seats_of_one_runtime_in_one_project_answer_nothing() -> None:
    """Because there is no way to tell them apart from here, and picking the
    first would route by the order somebody happened to write them in. Nothing
    means the caller falls back to routing by role, which at least knows it is
    approximating."""
    seats = [
        seat("onav", project="alpha-engine", role=Role.NAVIGATOR),
        seat("odrv", project="alpha-engine", role=Role.DRIVER),
    ]

    assert for_project(seats, "opencode", "alpha-engine") is None


def test_a_seat_with_no_project_is_never_matched() -> None:
    """The environment dialect cannot express a project and leaves it unset.
    Treating unset as "any" would make every such seat match every codebase."""
    seats = [seat("onav")]

    assert for_project(seats, "opencode", "alpha-engine") is None


def test_no_project_asked_about_matches_nothing() -> None:
    """A caller that does not know the codebase is not one that should be given
    a seat anyway."""
    seats = [seat("onav", project="alpha-engine")]

    assert for_project(seats, "opencode", None) is None
    assert for_project(seats, "opencode", "") is None
    assert for_project(seats, "opencode", "   ") is None


def test_the_project_is_matched_the_way_people_type_it() -> None:
    """Configuration is written by hand, and a card that went missing over a
    capital letter would be a bad afternoon."""
    seats = [seat("onav", project="Alpha-Engine")]

    assert for_project(seats, "opencode", " alpha-engine ") is seats[0]


def test_no_runtime_given_matches_on_the_project_alone() -> None:
    """For a caller that genuinely cannot know which runtime asked — the same
    allowance `for_session` makes, and with the same ambiguity rule."""
    seats = [seat("onav", project="alpha-engine")]

    assert for_project(seats, None, "alpha-engine") is seats[0]
