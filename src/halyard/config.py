"""Configuration, read from the environment once at startup.

Two choices here are load-bearing rather than stylistic.

**The channel must be named explicitly.** There is no default. One of the
available channels decides every approval by itself without asking anybody,
which is exactly what you want while testing the bridge and exactly what must
never happen by accident. A field with no default cannot be arrived at by
forgetting to set something.

**Binding is local by default.** The control plane holds the power to approve
commands on the machine it runs on. It has no business listening on a public
interface; reach it over Tailscale or WireGuard instead.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ChannelKind(StrEnum):
    """Which channel adapter resolves approvals."""

    #: Approves everything, immediately, without asking. For testing the bridge
    #: end to end before Telegram exists. Never for real use.
    STUB_ALLOW = "stub_allow"
    #: Denies everything, immediately. Useful for exercising the denial path.
    STUB_DENY = "stub_deny"
    #: Sends a card and waits for a human.
    TELEGRAM = "telegram"

    @property
    def decides_without_a_human(self) -> bool:
        return self in {ChannelKind.STUB_ALLOW, ChannelKind.STUB_DENY}


class Settings(BaseSettings):
    """Everything the control plane needs to run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bind: str = Field(default="127.0.0.1:8787", validation_alias="HALYARD_BIND")
    #: How long an approval card stays answerable.
    approval_timeout_seconds: int = Field(
        default=300, validation_alias="HALYARD_APPROVAL_TIMEOUT_SECONDS", gt=0
    )
    #: How long `hook_bridge.py` waits on its HTTP call. Must exceed the
    #: approval deadline, so the control plane is always the one that answers.
    bridge_timeout_seconds: int = Field(
        default=330, validation_alias="HALYARD_BRIDGE_TIMEOUT_SECONDS", gt=0
    )
    #: The `timeout` set on the hook in settings.json. Not read by Claude Code
    #: from here — declared so the ordering below can be checked at all.
    hook_timeout_seconds: int = Field(
        default=600, validation_alias="HALYARD_HOOK_TIMEOUT_SECONDS", gt=0
    )
    db_path: Path = Field(default=Path("./halyard.db"), validation_alias="HALYARD_DB_PATH")
    audit_log: Path = Field(default=Path("./audit.jsonl"), validation_alias="HALYARD_AUDIT_LOG")

    channel: ChannelKind = Field(validation_alias="HALYARD_CHANNEL")

    project_name: str = Field(default="unknown", validation_alias="CLAUDE_PROJECT_NAME")

    telegram_bot_token: str | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")
    #: `NoDecode` because pydantic-settings otherwise tries to JSON-decode any
    #: set-typed environment variable before a validator can see it. A single
    #: numeric id like `4242` is valid JSON, so it would arrive as an int; two
    #: ids like `4242,1337` are not valid JSON, so that would fail outright.
    #: Neither is a shape anyone writing a comma-separated list would expect.
    telegram_authorized_user_ids: Annotated[frozenset[str], NoDecode] = Field(
        default_factory=frozenset, validation_alias="TELEGRAM_AUTHORIZED_USER_IDS"
    )

    #: Where a navigator's and a driver's traffic goes, when you want them apart.
    #: Both optional: leave them unset and everything lands in TELEGRAM_CHAT_ID,
    #: exactly as before.
    #:
    #: Two seats, deliberately — not an open-ended map of role to destination.
    #: A third session takes over one of these rather than adding a third place
    #: to look, which is the point of splitting them at all.
    #:
    #: Each is a chat id, optionally with a forum topic after a colon:
    #:     -1001234567890        a group of its own
    #:     -1001234567890:12     topic 12 inside a shared group
    telegram_navigator_chat_id: str | None = Field(
        default=None, validation_alias="TELEGRAM_NAVIGATOR_CHAT_ID"
    )
    telegram_driver_chat_id: str | None = Field(
        default=None, validation_alias="TELEGRAM_DRIVER_CHAT_ID"
    )

    #: Which named session sits in which seat. This is how the desktop app is
    #: told apart: there is no shell there to set HALYARD_ROLE in, but every
    #: session has a name, and that name survives restarts where session_id does
    #: not. Copy them exactly — `halyard sessions` lists what it can see.
    navigator_session: str | None = Field(
        default=None, validation_alias="HALYARD_NAVIGATOR_SESSION"
    )
    driver_session: str | None = Field(default=None, validation_alias="HALYARD_DRIVER_SESSION")

    #: Model names offered by /options, comma separated. Only a suggestion —
    #: anything is passed through to the CLI — but worth being able to update
    #: without waiting for a release, because models ship faster than this does.
    claude_models: str | None = Field(default=None, validation_alias="HALYARD_CLAUDE_MODELS")

    #: What a turn started from a chat runs on before anybody says otherwise.
    #: Not the CLI's own default, which is haiku: a message sent from a phone
    #: continues real work in a real codebase, and picking the cheapest model
    #: for it silently is the kind of default that is only noticed in the
    #: quality of the answer. Set it empty to pass no `--model` at all.
    claude_default_model: str = Field(
        default="sonnet", validation_alias="HALYARD_CLAUDE_DEFAULT_MODEL"
    )

    @model_validator(mode="after")
    def _timeouts_must_be_ordered(self) -> Settings:
        """Refuse to start unless approval < bridge < hook.

        A hook that outruns its timeout fails open — Claude Code discards it and
        runs the command. That was measured, not assumed. So every layer has to
        answer before the one above it gives up:

            approval deadline  <  bridge HTTP timeout  <  hook timeout

        Get this backwards and nothing looks wrong. Approvals work, denials
        work, the tests pass. The only visible symptom is that a request nobody
        answers in time quietly executes instead of being denied, which is the
        one case the whole system exists for.
        """
        if not (
            self.approval_timeout_seconds < self.bridge_timeout_seconds < self.hook_timeout_seconds
        ):
            raise ValueError(
                "Timeouts must satisfy HALYARD_APPROVAL_TIMEOUT_SECONDS < "
                "HALYARD_BRIDGE_TIMEOUT_SECONDS < HALYARD_HOOK_TIMEOUT_SECONDS, but got "
                f"{self.approval_timeout_seconds} < {self.bridge_timeout_seconds} < "
                f"{self.hook_timeout_seconds}. A hook that exceeds its timeout fails open, "
                "so an unanswered request would run instead of being denied."
            )
        return self

    @field_validator("telegram_authorized_user_ids", mode="before")
    @classmethod
    def _split_user_ids(cls, value: object) -> object:
        """Accept a comma-separated list, which is how a person writes this.

        Ints are accepted too. A Telegram user id *is* a number, and somebody
        setting one in code rather than in the environment will reach for one.
        """
        if isinstance(value, str | int):
            return frozenset(part.strip() for part in str(value).split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _telegram_needs_its_credentials(self) -> Settings:
        if self.channel is not ChannelKind.TELEGRAM:
            return self
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
                ("TELEGRAM_AUTHORIZED_USER_IDS", self.telegram_authorized_user_ids),
            )
            if not value
        ]
        if missing:
            # Starting without an authorized user list would mean either nobody
            # can approve anything or, worse, that the check was skipped.
            raise ValueError(f"HALYARD_CHANNEL=telegram requires {', '.join(missing)} to be set")
        return self

    @property
    def host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])
