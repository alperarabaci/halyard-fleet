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

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    approval_timeout_seconds: int = Field(
        default=300, validation_alias="HALYARD_APPROVAL_TIMEOUT_SECONDS", gt=0
    )
    db_path: Path = Field(default=Path("./halyard.db"), validation_alias="HALYARD_DB_PATH")
    audit_log: Path = Field(default=Path("./audit.jsonl"), validation_alias="HALYARD_AUDIT_LOG")

    channel: ChannelKind = Field(validation_alias="HALYARD_CHANNEL")

    project_name: str = Field(default="unknown", validation_alias="CLAUDE_PROJECT_NAME")

    telegram_bot_token: str | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")
    telegram_authorized_user_ids: frozenset[str] = Field(
        default_factory=frozenset, validation_alias="TELEGRAM_AUTHORIZED_USER_IDS"
    )

    @field_validator("telegram_authorized_user_ids", mode="before")
    @classmethod
    def _split_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
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
