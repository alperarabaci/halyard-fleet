"""Tests for configuration loading."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from halyard.config import ChannelKind, Settings


def build(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_the_channel_must_be_named() -> None:
    # No default, on purpose: one of the channels answers every request by
    # itself, and that must never be arrived at by forgetting to set something.
    with pytest.raises(ValidationError):
        build()


def test_the_defaults_keep_the_service_local() -> None:
    settings = build(HALYARD_CHANNEL="stub_deny")

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787
    assert settings.approval_timeout_seconds == 300


def test_telegram_will_not_start_without_its_credentials() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_"):
        build(HALYARD_CHANNEL="telegram", TELEGRAM_BOT_TOKEN="t")


def test_telegram_will_not_start_without_an_authorized_user() -> None:
    # Starting with an empty list would mean either nobody can approve anything
    # or, worse, that the check was skipped.
    with pytest.raises(ValidationError, match="TELEGRAM_AUTHORIZED_USER_IDS"):
        build(
            HALYARD_CHANNEL="telegram",
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            TELEGRAM_AUTHORIZED_USER_IDS="",
        )


def test_telegram_starts_when_fully_configured() -> None:
    settings = build(
        HALYARD_CHANNEL="telegram",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        TELEGRAM_AUTHORIZED_USER_IDS="4242, 1337 ,",
    )

    assert settings.telegram_authorized_user_ids == {"4242", "1337"}


def test_stub_channels_declare_that_nobody_is_asked() -> None:
    assert ChannelKind.STUB_ALLOW.decides_without_a_human
    assert ChannelKind.STUB_DENY.decides_without_a_human
    assert not ChannelKind.TELEGRAM.decides_without_a_human


def test_a_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(HALYARD_CHANNEL="stub_deny", HALYARD_APPROVAL_TIMEOUT_SECONDS="0")
