"""Tests for the choice cards `cards.py` builds — the part every menu shares."""

from __future__ import annotations

from halyard.channels.telegram import cards


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
        cards.check_choices(("proof",)),
    ]

    for keyboard in built:
        assert keyboard["inline_keyboard"][-1] == [cards.CANCEL]


def test_the_cancel_button_is_read_back_as_a_choice() -> None:
    assert cards.parse_choice_data(cards.CANCEL["callback_data"]) == ("cancel", "x")
