"""Tests for configuration loading."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from halyard.config import ChannelKind, Settings


def build(**env: str) -> Settings:
    """Construct from keyword arguments.

    Convenient, but note that this bypasses how settings are actually supplied.
    Anything about *parsing* an environment variable has to go through
    `from_env` below — keyword arguments skip the decoding step where
    pydantic-settings does its own thing to complex types.
    """
    return Settings(_env_file=None, **env)


def from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Construct the way the running service does: from the environment."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


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
    assert settings.claude_binary is None
    assert settings.claude_default_model == ""


def test_the_claude_binary_can_be_pinned() -> None:
    settings = build(
        HALYARD_CHANNEL="stub_deny",
        HALYARD_CLAUDE_BINARY="/Applications/Claude.app/Contents/claude",
    )

    assert settings.claude_binary == "/Applications/Claude.app/Contents/claude"


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


# --- reading the authorized user list from the environment ------------------
#
# These go through the environment rather than through keyword arguments,
# because that is where the bug was: pydantic-settings JSON-decodes set-typed
# environment variables before any validator sees them, and every realistic
# value of this one is either valid JSON meaning the wrong thing, or not JSON at
# all. Tests that passed the value as a keyword argument never touched that step
# and so agreed with a field that could not load.


def test_a_single_user_id_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = from_env(
        monkeypatch,
        HALYARD_CHANNEL="telegram",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        # Valid JSON, and JSON says this is the number 4242.
        TELEGRAM_AUTHORIZED_USER_IDS="4242",
    )

    assert settings.telegram_authorized_user_ids == {"4242"}


def test_several_user_ids_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = from_env(
        monkeypatch,
        HALYARD_CHANNEL="telegram",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        # Not valid JSON at all.
        TELEGRAM_AUTHORIZED_USER_IDS="4242,1337",
    )

    assert settings.telegram_authorized_user_ids == {"4242", "1337"}


def test_a_sloppily_written_list_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = from_env(
        monkeypatch,
        HALYARD_CHANNEL="telegram",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        TELEGRAM_AUTHORIZED_USER_IDS=" 4242 , 1337 , ",
    )

    assert settings.telegram_authorized_user_ids == {"4242", "1337"}


def test_an_empty_user_list_in_the_environment_still_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_AUTHORIZED_USER_IDS"):
        from_env(
            monkeypatch,
            HALYARD_CHANNEL="telegram",
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            TELEGRAM_AUTHORIZED_USER_IDS="",
        )


def test_the_whole_configuration_loads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = from_env(
        monkeypatch,
        HALYARD_CHANNEL="telegram",
        HALYARD_BIND="127.0.0.1:8799",
        HALYARD_APPROVAL_TIMEOUT_SECONDS="120",
        HALYARD_BRIDGE_TIMEOUT_SECONDS="150",
        HALYARD_HOOK_TIMEOUT_SECONDS="300",
        CLAUDE_PROJECT_NAME="alpha-engine",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="-1001234567890",
        TELEGRAM_AUTHORIZED_USER_IDS="4242",
    )

    assert settings.port == 8799
    assert settings.project_name == "alpha-engine"
    assert settings.telegram_chat_id == "-1001234567890"
    assert settings.approval_timeout_seconds == 120


def test_stub_channels_declare_that_nobody_is_asked() -> None:
    assert ChannelKind.STUB_ALLOW.decides_without_a_human
    assert ChannelKind.STUB_DENY.decides_without_a_human
    assert not ChannelKind.TELEGRAM.decides_without_a_human


def test_a_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(HALYARD_CHANNEL="stub_deny", HALYARD_APPROVAL_TIMEOUT_SECONDS="0")


# --- the timeout ordering ---------------------------------------------------


def test_the_default_timeouts_are_ordered() -> None:
    settings = build(HALYARD_CHANNEL="stub_deny")

    assert (
        settings.approval_timeout_seconds
        < settings.bridge_timeout_seconds
        < settings.hook_timeout_seconds
    )


@pytest.mark.parametrize(
    ("approval", "bridge", "hook"),
    [
        ("400", "330", "600"),  # the bridge gives up before the approver does
        ("300", "700", "600"),  # the hook gives up before the bridge does
        ("300", "300", "600"),  # a tie is not an ordering
        ("600", "600", "600"),  # everything at once
    ],
)
def test_a_broken_ordering_refuses_to_start(approval: str, bridge: str, hook: str) -> None:
    # Get this wrong and nothing looks broken: approvals work, denials work, the
    # tests pass. The only symptom is that an unanswered request quietly runs
    # instead of being denied — the one case the system exists for.
    with pytest.raises(ValidationError, match="fails open"):
        build(
            HALYARD_CHANNEL="stub_deny",
            HALYARD_APPROVAL_TIMEOUT_SECONDS=approval,
            HALYARD_BRIDGE_TIMEOUT_SECONDS=bridge,
            HALYARD_HOOK_TIMEOUT_SECONDS=hook,
        )


def test_a_valid_custom_ordering_is_accepted() -> None:
    settings = build(
        HALYARD_CHANNEL="stub_deny",
        HALYARD_APPROVAL_TIMEOUT_SECONDS="60",
        HALYARD_BRIDGE_TIMEOUT_SECONDS="90",
        HALYARD_HOOK_TIMEOUT_SECONDS="120",
    )

    assert settings.approval_timeout_seconds == 60
