"""Configuration, read once at startup from `halyard.yaml`.

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
from typing import Annotated, Any

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


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


class YamlSettings(PydanticBaseSettingsSource):
    """The `settings:` block of `halyard.yaml`, read as a settings source.

    **One file describes a machine.** Seats moved to YAML first and everything
    else stayed in `.env`, which left two files that each had to be edited to
    change one thing and no rule saying which won. A person configuring this
    had to know both.

    Keys are the environment names — `TELEGRAM_BOT_TOKEN`, `HALYARD_BIND` —
    rather than new ones. They are what every message, every doctor line and
    every existing installation already says, and inventing a second vocabulary
    for the same settings would be the same mistake in a smaller font.

    A real environment variable still wins. That is not a second file; it is
    how a container passes a secret in, and taking it away would mean writing
    the token to disk is the only way to run this.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None = None) -> None:
        super().__init__(settings_cls)
        self._path = path

    def _values(self) -> dict[str, Any]:
        from halyard.core.config_file import find_config

        path = self._path or find_config()
        if path is None:
            return {}
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # A file that cannot be read is reported by `halyard doctor`, which
            # says which file and why. Refusing to start here would turn one
            # bad line into a control plane that cannot say what is wrong.
            return {}
        block = document.get("settings") if isinstance(document, dict) else None
        return {str(k): v for k, v in block.items()} if isinstance(block, dict) else {}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        alias = str(field.validation_alias or field_name)
        values = self._values()
        return values.get(alias), alias, False

    def __call__(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, name)
            if value is not None:
                found[key] = value
        return found


class Settings(BaseSettings):
    """Everything the control plane needs to run."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Arguments, then the environment, then `halyard.yaml`.

        `.env` is gone. It was the second of two files describing one machine,
        and which of them won was not written down anywhere.
        """
        return (init_settings, env_settings, YamlSettings(settings_cls))

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

    #: Where the running log is kept, as opposed to the audit log beside it.
    #: They answer different questions and neither replaces the other: the audit
    #: log records decisions, this one records what the process was doing when
    #: it made them — and, more to the point, what it was doing when it made
    #: none. On by default, because the moment you want it is always in the past.
    #: Set it empty to log only to the console.
    #:
    #: In a folder, and a new file each week. One flat file reached thirty-six
    #: thousand lines in a month, which is not something anybody reads — and the
    #: unit a person actually looks in is the week. The bridge writes its own
    #: weekly files into the same folder, so everything about one week is in one
    #: place.
    log_file: Path | None = Field(
        default=Path("./logs/halyard.log"), validation_alias="HALYARD_LOG_FILE"
    )
    #: Whether to stop the machine drifting off to sleep while serving.
    #:
    #: On, because a wired project cannot run a command without an answer from
    #: this process — a sleeping control plane is every session on the machine
    #: stopped, and it does not announce itself. Measured on a Mac mini whose
    #: only wake assertion belonged to a screen-sharing session: when that
    #: connection dropped the machine slept and approvals began appearing in
    #: the desktop app instead of on a phone.
    #:
    #: Idle sleep only. A person can still close the lid or choose Sleep.
    keep_awake: bool = Field(default=True, validation_alias="HALYARD_KEEP_AWAKE")

    log_level: str = Field(default="INFO", validation_alias="HALYARD_LOG_LEVEL")
    #: A ceiling on one week, so an always-on service cannot fill a disk before
    #: the next Monday. Reaching it rolls early and keeps both halves.
    log_max_bytes: int = Field(default=5_000_000, validation_alias="HALYARD_LOG_MAX_BYTES", gt=0)
    #: How many past weeks to keep — of the running log, and of the bridge's own
    #: weekly files beside it. Two months, which is longer than any question
    #: about "when did this start" has needed so far.
    log_backups: int = Field(default=8, validation_alias="HALYARD_LOG_BACKUPS", ge=0)

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

    #: Which model writes the record carried across a compaction. A distillation
    #: of text somebody else already wrote — the reasoning happened in the
    #: session, not here — so it does not need the expensive one.
    compaction_model: str = Field(default="sonnet", validation_alias="HALYARD_COMPACTION_MODEL")

    #: How much of the compaction record may be carried into the fresh context.
    #: Measured in the field: the model fills very nearly whatever it is given,
    #: and what is carried across is what the next compaction arrives sooner for.
    compaction_record_limit: int = Field(
        default=2_000, validation_alias="HALYARD_COMPACTION_RECORD_LIMIT", gt=0
    )

    #: Which Claude Code executable sends turns received from a channel.
    #: On macOS the runner otherwise prefers the engine bundled with Claude
    #: Desktop, keeping the writer and the already-open session on one version.
    claude_binary: str | None = Field(default=None, validation_alias="HALYARD_CLAUDE_BINARY")

    #: Optional override for turns started from a channel. Empty preserves the
    #: resumed session's model; this was measured on a Desktop-owned opus
    #: session. A value here deliberately replaces that model.
    claude_default_model: str = Field(default="", validation_alias="HALYARD_CLAUDE_DEFAULT_MODEL")

    #: A long-lived credential for turns this control plane starts, so they do
    #: not depend on the desktop login that expires.
    #:
    #: The login `/login` creates is refreshed while somebody is at the keyboard
    #: and eventually cannot be — measured twice, four days apart, each time
    #: stopping remote work with "OAuth session expired and could not be
    #: refreshed" until somebody signed in at the desk. That is exactly the
    #: situation Halyard exists to survive.
    #:
    #: Mint one with `claude setup-token`: it uses the subscription rather than
    #: pay-as-you-go API billing, and lasts about a year. Passed to the CLI as
    #: `CLAUDE_CODE_OAUTH_TOKEN` for the turns Halyard starts and nothing else —
    #: a session someone drives at the keyboard is untouched by this.
    #:
    #: Secret, like the bot token, and lives in the same gitignored file.
    claude_oauth_token: str | None = Field(
        default=None, validation_alias="HALYARD_CLAUDE_OAUTH_TOKEN"
    )
    #: For reaching an issue tracker — today only to add a label to the task a
    #: branch is for. Named for the idea rather than for GitLab, because the
    #: provider is chosen by the repository's remote and a second one is a
    #: module rather than a rename. Absent means `/label` says so and every
    #: other command is unaffected.
    #:
    #: GitLab has no scope narrower than `api` for writing a label onto an
    #: issue, so a *project* access token is the way to make that mean less.
    forge_token: str | None = Field(default=None, validation_alias="HALYARD_FORGE_TOKEN")

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

    @field_validator("log_level", mode="before")
    @classmethod
    def _known_level(cls, value: object) -> object:
        """Refuse a level that does not exist rather than quietly using INFO.

        A typo here is invisible in the worst way: `HALYARD_LOG_LEVEL=DEBUGG`
        would leave you reading an INFO log while believing you had turned
        debugging on, and concluding from its silence that nothing happened.
        """
        if isinstance(value, str):
            level = value.strip().upper()
            if level not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
                raise ValueError(
                    f"HALYARD_LOG_LEVEL={value!r} is not a level. "
                    "Use one of CRITICAL, ERROR, WARNING, INFO, DEBUG."
                )
            return level
        return value

    @field_validator("log_file", mode="before")
    @classmethod
    def _empty_means_console_only(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

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
