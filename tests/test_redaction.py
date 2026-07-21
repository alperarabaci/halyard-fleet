"""Tests for secret redaction."""

from __future__ import annotations

import pytest

from halyard.core.redaction import Redactor, summarize

SECRET = "hunter2SuperSecretValue"


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


# --- what gets masked -------------------------------------------------------


def test_credentials_in_a_url_are_masked(redactor: Redactor) -> None:
    result = redactor.redact("psql postgres://alper:hunter2@db.internal:5432/alpha")

    assert result.text == "psql postgres://***:***@db.internal:5432/alpha"
    assert "url_credentials" in result.applied


@pytest.mark.parametrize(
    "name",
    [
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "DB_PASSWORD",
        "STRIPE_API_KEY",
        "MY_APP_SECRET",
        "SSH_PASSPHRASE",
    ],
)
def test_sensitive_assignments_are_masked(redactor: Redactor, name: str) -> None:
    result = redactor.redact(f"export {name}={SECRET}")

    # The name survives on purpose: an approver who cannot tell what they are
    # approving will either rubber-stamp everything or refuse everything.
    assert result.text == f"export {name}=***"
    assert SECRET not in result.text


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_values_are_masked_whole(redactor: Redactor, quote: str) -> None:
    result = redactor.redact(f"DB_PASSWORD={quote}two words here{quote}")

    assert result.text == "DB_PASSWORD=***"


@pytest.mark.parametrize("command", ["MONKEY=1", "TURKEY=2", "KEYBOARD_LAYOUT=us"])
def test_innocent_names_that_merely_contain_key_are_left_alone(
    redactor: Redactor, command: str
) -> None:
    assert redactor.redact(command).text == command


def test_sensitive_flags_are_masked(redactor: Redactor) -> None:
    assert redactor.redact(f"mysql -h db --password {SECRET}").text == (
        "mysql -h db --password ***"
    )
    assert redactor.redact(f"deploy --token={SECRET}").text == "deploy --token=***"


def test_basic_auth_credentials_keep_the_user_and_drop_the_password(
    redactor: Redactor,
) -> None:
    result = redactor.redact(f"curl -u alper:{SECRET} https://api.internal")

    assert result.text == "curl -u alper:*** https://api.internal"


def test_authorization_headers_are_masked(redactor: Redactor) -> None:
    result = redactor.redact(f'curl -H "Authorization: Bearer {SECRET}" https://api.internal')

    assert SECRET not in result.text
    assert "Bearer ***" in result.text


@pytest.mark.parametrize(
    ("token", "kept"),
    [
        ("ghp_" + "a" * 36, "ghp_"),
        ("sk-" + "b" * 32, "sk-"),
        ("xoxb-" + "1" * 12 + "-abcdef", "xox-"),
        ("AKIA" + "A" * 16, "AKIA"),
    ],
)
def test_known_token_formats_are_masked_but_still_identifiable(
    redactor: Redactor, token: str, kept: str
) -> None:
    result = redactor.redact(f"git remote add origin https://{token}@github.com/a/b")

    assert token not in result.text
    # The prefix stays so the approver can see what kind of credential it was.
    assert kept in result.text


def test_private_key_blocks_collapse_to_their_markers(redactor: Redactor) -> None:
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxGZ8v1qk\nQm5nR2VuZXJhdGVk\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redactor.redact(f"echo '{key}' > id_rsa")

    assert "MIIEowIBAAKCAQEAxGZ8v1qk" not in result.text
    assert "-----BEGIN RSA PRIVATE KEY----- *** -----END RSA PRIVATE KEY-----" in result.text


def test_a_jwt_is_masked(redactor: Redactor) -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    result = redactor.redact(f"curl -H 'X-Auth: {jwt}' https://api.internal")

    assert jwt not in result.text


# --- what does not get masked -----------------------------------------------


def test_an_ordinary_command_is_untouched(redactor: Redactor) -> None:
    result = redactor.redact("git status --short")

    assert result.text == "git status --short"
    assert result.applied == ()
    assert not result.redacted


def test_masking_is_stable_when_applied_twice(redactor: Redactor) -> None:
    once = redactor.redact(f"DB_PASSWORD={SECRET}").text

    assert redactor.redact(once).text == once


# --- reporting --------------------------------------------------------------


def test_applied_names_rules_and_never_values(redactor: Redactor) -> None:
    result = redactor.redact(f"psql postgres://a:{SECRET}@db/x --password {SECRET}")

    assert set(result.applied) == {"url_credentials", "sensitive_flag"}
    # The report exists so the audit log can say a secret was masked without
    # becoming the place the secret ended up instead.
    assert all(SECRET not in name for name in result.applied)
    assert result.redacted


# --- preparing a command for a card -----------------------------------------


def test_prepare_masks_before_it_truncates(redactor: Redactor) -> None:
    command = f"DB_PASSWORD={SECRET} && " + "echo padding && " * 40
    prepared = redactor.prepare(command, summary_limit=60)

    # Truncating first and masking second would leak whatever survived the cut.
    # Here the secret sits well inside the limit, so that ordering would put it
    # straight onto the card.
    assert SECRET not in prepared.summary
    assert SECRET not in prepared.full
    assert prepared.summary.startswith("DB_PASSWORD=***")
    assert prepared.truncated
    assert prepared.redacted


def test_prepare_keeps_a_short_command_whole(redactor: Redactor) -> None:
    prepared = redactor.prepare("git status")

    assert prepared.full == prepared.summary == "git status"
    assert not prepared.truncated
    assert not prepared.redacted


def test_prepare_keeps_the_full_command_untruncated(redactor: Redactor) -> None:
    command = "echo " + "x" * 500
    prepared = redactor.prepare(command, summary_limit=50)

    assert len(prepared.full) == len(command)
    assert len(prepared.summary) <= 50


# --- summarizing ------------------------------------------------------------


def test_summarize_collapses_whitespace() -> None:
    assert summarize("git   status\n  --short\t\t-b") == "git status --short -b"


def test_summarize_marks_where_it_cut() -> None:
    assert summarize("x" * 100, limit=10) == "x" * 9 + "…"


def test_summarize_leaves_short_text_alone() -> None:
    assert summarize("git status", limit=100) == "git status"


# --- keeping secrets out of the log -----------------------------------------


def test_a_telegram_bot_token_in_a_url_is_masked(redactor: Redactor) -> None:
    line = "HTTP Request: POST https://api.telegram.org/bot8683402306:AAFAKEfake_TOKEN-value-here-1234567/getUpdates"

    result = redactor.redact(line)

    assert "AAFAKEfake_TOKEN-value-here-1234567" not in result.text
    # The numeric bot id survives: it names the bot without granting anything.
    assert "bot8683402306:***" in result.text


def test_the_log_filter_masks_secrets_a_library_prints(caplog) -> None:
    import logging

    from halyard.core.redaction import SecretRedactingFilter

    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: POST https://api.telegram.org/bot123456789:%s/getUpdates",
        args=("AAFAKEfake_TOKEN-value-here-1234567",),
        exc_info=None,
    )

    assert handler.filters[0].filter(record) is True
    # Overriding TelegramApi.__repr__ kept the token out of tracebacks and httpx
    # printed it anyway. Keeping a secret out of your own log lines is not the
    # same as keeping it out of the log.
    assert "AAFAKEfake_TOKEN-value-here-1234567" not in record.getMessage()
    assert "bot123456789:***" in record.getMessage()
