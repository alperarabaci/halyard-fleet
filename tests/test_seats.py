"""Seats — every one you have configured, available at the same time.

The design this replaces made runtime a property of a role, fixed at startup.
That meant a driver was Claude Code or Codex, and changing it was an edit, a
restart of the control plane, and probably a restart of the desktop apps — at
exactly the moment you would least want to do any of that, which is a quota
running out while you are away from the machine.
"""

from __future__ import annotations

import pytest

from halyard.core import seats
from halyard.core.events import Role

FOUR = {
    "HALYARD_SEATS": "nav,drv,xnav,xdrv",
    "HALYARD_SEAT_NAV": "runtime=claude-code session=alpha-navigator chat=-1001 role=navigator",
    "HALYARD_SEAT_DRV": "runtime=claude-code session=alpha-driver chat=-1002 role=driver",
    "HALYARD_SEAT_XNAV": "runtime=codex session=codex-nav chat=-1003 role=navigator",
    "HALYARD_SEAT_XDRV": "runtime=codex session=codex-drv chat=-1004:12 role=driver",
}


def test_both_runtimes_are_live_at_once() -> None:
    """The requirement the previous design could not meet.

    Two Claude seats and two Codex seats, all reachable, so which one does a
    piece of work is decided when the message is sent rather than when the
    process started.
    """
    configured = seats.from_environment(FOUR)

    assert [s.label for s in configured] == ["nav", "drv", "xnav", "xdrv"]
    assert {s.runtime for s in configured} == {"claude-code", "codex"}


def test_a_seat_carries_its_own_session_and_destination() -> None:
    configured = seats.from_environment(FOUR)

    codex_driver = seats.find(configured, "xdrv")

    assert codex_driver.runtime == "codex"
    assert codex_driver.session == "codex-drv"
    assert codex_driver.chat == "-1004:12"
    assert codex_driver.role is Role.DRIVER


def test_a_seat_is_found_however_it_is_typed() -> None:
    """Typed by hand, on a phone, usually while doing something else."""
    configured = seats.from_environment(FOUR)

    assert seats.find(configured, "XDRV") is not None
    assert seats.find(configured, "  xdrv  ") is not None
    assert seats.find(configured, "xdrvv") is None


def test_two_seats_can_share_a_group_through_topics() -> None:
    """A forum topic is a destination of its own, so `-1004` and `-1004:12`
    are different places that live in one group."""
    configured = seats.from_environment(FOUR)

    assert seats.for_chat(configured, "-1004:12").label == "xdrv"
    assert seats.for_chat(configured, "-1003").label == "xnav"


def test_a_configuration_written_before_any_of_this_still_means_the_same() -> None:
    """Nobody should have to rewrite a working setup to keep it working."""
    configured = seats.from_environment(
        {
            "HALYARD_NAVIGATOR_SESSION": "alpha-navigator",
            "TELEGRAM_NAVIGATOR_CHAT_ID": "-1001",
            "HALYARD_DRIVER_SESSION": "alpha-driver",
            "TELEGRAM_DRIVER_CHAT_ID": "-1002",
        }
    )

    assert [(s.label, s.runtime, s.role) for s in configured] == [
        ("navigator", "claude-code", Role.NAVIGATOR),
        ("driver", "claude-code", Role.DRIVER),
    ]


def test_nothing_configured_is_no_seats_rather_than_an_error() -> None:
    assert seats.from_environment({}) == []


def test_an_unknown_runtime_is_refused() -> None:
    """Falling back would send you hunting a naming mistake that is not there:
    a seat meant as Codex would look for a Claude Code session and report it
    missing."""
    with pytest.raises(ValueError, match="Use one of"):
        seats.from_environment(
            {"HALYARD_SEATS": "x", "HALYARD_SEAT_X": "runtime=gpt session=whatever"}
        )


def test_a_seat_named_but_not_described_is_refused() -> None:
    """Silence here would leave a seat you believe exists and cannot reach."""
    with pytest.raises(ValueError, match="HALYARD_SEAT_GHOST is not set"):
        seats.from_environment({"HALYARD_SEATS": "ghost"})


def test_a_mistyped_field_is_refused_rather_than_ignored() -> None:
    """Ignoring it would leave the seat missing the setting you thought you
    gave it, with nothing anywhere saying so."""
    with pytest.raises(ValueError, match="unknown field"):
        seats.from_environment(
            {"HALYARD_SEATS": "x", "HALYARD_SEAT_X": "runtime=codex sesion=typo"}
        )


def test_a_positional_looking_spec_is_refused_with_an_example() -> None:
    with pytest.raises(ValueError, match="key=value"):
        seats.from_environment({"HALYARD_SEATS": "x", "HALYARD_SEAT_X": "codex:my-thread:-1001"})


def test_a_label_with_a_dash_maps_to_an_environment_name() -> None:
    configured = seats.from_environment(
        {"HALYARD_SEATS": "codex-drv", "HALYARD_SEAT_CODEX_DRV": "runtime=codex session=t"}
    )

    assert configured[0].label == "codex-drv"


def test_a_seat_needs_no_destination_to_be_addressable() -> None:
    """Reachable by name from anywhere, with nowhere of its own to speak —
    which is enough to hand it a prompt."""
    configured = seats.from_environment(
        {"HALYARD_SEATS": "spare", "HALYARD_SEAT_SPARE": "runtime=codex session=t"}
    )

    assert seats.find(configured, "spare").chat is None
