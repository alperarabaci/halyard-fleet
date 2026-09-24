"""Tests for the choice cards `cards.py` builds — the part every menu shares."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from halyard.channels.telegram import cards
from halyard.core.approvals import ApprovalRequest
from halyard.core.events import RiskLevel

NOW = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)


def a_command(**overrides: object) -> ApprovalRequest:
    defaults = {
        "request_id": "req_1",
        "nonce": "nonce-1",
        "session_id": "0b7c6f0e-5d7a-4c1e-9a53-2b1f4b9c8d11",
        "agent_id": "claude-code",
        "project": "alpha-engine",
        "tool": "Bash",
        "command_summary": "make test-fast",
        "command_full": "make test-fast",
        "risk": RiskLevel.HIGH,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    return ApprovalRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_every_choice_card_can_be_cancelled() -> None:
    """Only the commit card could be closed without choosing; every other menu
    stayed in the chat, still pressable long after anybody meant to."""
    built = [
        cards.choices("model", ("opus", "sonnet")),
        cards.forward_choices(("nav", "drv")),
        cards.seat_choices(("nav", "drv")),
        cards.open_choices(("claude", "codex")),
        cards.command_choices(("test-all",)),
        cards.label_choices(("andon",)),
        cards.inspection_choices(("proof",)),
        cards.result_choices("proof", ("nav", "drv")),
        cards.transition_choices(("review", "discovery")),
        cards.transition_seat_choices("review", ("nav", "xrev")),
    ]

    for keyboard in built:
        assert keyboard["inline_keyboard"][-1] == [cards.CANCEL]


def test_a_result_button_carries_the_check_as_well_as_the_seat() -> None:
    """A chat holds several checks' answers, and the button under one of them
    must send that one."""
    keyboard = cards.result_choices("proof", ("nav",))

    data = keyboard["inline_keyboard"][0][0]["callback_data"]
    assert cards.parse_choice_data(data) == ("result", "proof>nav")


def test_the_cancel_button_is_read_back_as_a_choice() -> None:
    assert cards.parse_choice_data(cards.CANCEL["callback_data"]) == ("cancel", "x")


def test_a_command_an_inspection_asks_for_says_whose_it_is() -> None:
    """An inspection's turn is nobody's seat. Without this the card would say
    AGENT over a session nobody has seen, and what is being allowed is an
    inspection looking, not a seat working — before it is answered and after."""
    inspection = "claims · transition discover_completed"

    asked = cards.render(a_command(), now=NOW, inspection=inspection)
    settled = cards.render_resolved(
        a_command(), decision="allow", by="tg:4242", inspection=inspection
    )

    assert asked.startswith("<b>[INSPECTION — PERMISSION REQUEST]</b>")
    assert "Inspection: <b>claims · transition discover_completed</b>" in asked
    assert "Inspection: <b>claims · transition discover_completed</b>" in settled


def test_a_seats_command_is_carded_as_it_always_was() -> None:
    asked = cards.render(a_command(), now=NOW)

    assert asked.startswith("<b>[AGENT — PERMISSION REQUEST]</b>")
    assert "Inspection:" not in asked


def test_only_an_inspections_card_can_stop_the_inspection() -> None:
    """Deny refuses one command and an inspection tries the next; Stop ends it.
    A seat's card has no such button — a seat is not stopped from a card."""
    seats = cards.keyboard(a_command(), include_full=False)
    inspecting = cards.keyboard(a_command(), include_full=False, stoppable=True)

    assert [key["text"] for row in seats["inline_keyboard"] for key in row] == [
        "Allow once",
        "Deny",
    ]
    [stop] = inspecting["inline_keyboard"][-1]
    assert stop["text"] == "⏹ Stop the inspection"
    assert cards.parse_callback_data(stop["callback_data"])[2] == cards.STOP


def test_a_stopped_card_says_who_stopped_it() -> None:
    settled = cards.render_resolved(a_command(), decision="stop", by="tg:4242", inspection="claims")

    assert settled.startswith("<b>⏹ STOPPED</b> by tg:4242")
    assert "Inspection: <b>claims</b>" in settled
