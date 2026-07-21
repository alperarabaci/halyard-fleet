"""Masking secrets before they leave the machine that owns them.

An approval card travels to Telegram, which means it travels through somebody
else's servers and lands in a chat history neither of us controls. A command
that names a password puts that password there permanently. So redaction runs at
the edge — the moment a payload enters core, before an `ApprovalRequest` is
built — and every layer downstream is free to assume it has already happened.

Two things this module is deliberate about:

**Masking keeps the shape of what it hides.** `AWS_SECRET_ACCESS_KEY=***` tells
the approver a credential is being set and which one; a bare `***` tells them
nothing, and an approver who cannot tell what they are approving will either
rubber-stamp it or refuse everything. The name stays, the value goes.

**Truncation cannot be separated from masking.** Shortening a command for a card
and then masking it would leak whatever the cut happened to preserve, so
`Redactor.prepare()` does both in the only safe order and hands back the pair.
There is no public way to get a truncated string that has not been masked first.

Rules are data. Adding a token format is a line in a list, not a new branch in a
function nobody wants to touch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

#: What a masked value is replaced with. Short, obviously not a real value, and
#: safe to paste into a chat message.
MASK = "***"

#: How much of a command a summary card shows before it is cut.
DEFAULT_SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class RedactionRule:
    """One pattern and what to put in its place."""

    name: str
    pattern: re.Pattern[str]
    replacement: str


def _rule(name: str, pattern: str, replacement: str, flags: int = 0) -> RedactionRule:
    return RedactionRule(name=name, pattern=re.compile(pattern, flags), replacement=replacement)


#: Order matters. Structural matches run before the general ones, so a private
#: key is recognised as a block rather than shredded by a rule looking for
#: assignments inside it.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    # A PEM block, collapsed but still identifiable.
    _rule(
        "private_key_block",
        r"-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----.*?-----END (?P=kind)-----",
        rf"-----BEGIN \g<kind>----- {MASK} -----END \g<kind>-----",
        re.DOTALL,
    ),
    # Credentials embedded in a URL: postgres://user:pass@host.
    _rule(
        "url_credentials",
        r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]+):(?P<secret>[^@/\s]+)@",
        rf"\g<scheme>{MASK}:{MASK}@",
    ),
    # NAME=value and NAME: value, where NAME ends in something sensitive. The
    # lookbehind is what keeps MONKEY=1 out of it.
    _rule(
        "sensitive_assignment",
        r"(?<![A-Za-z0-9])"
        r"(?P<name>(?:[A-Za-z0-9]+[_\-])*"
        r"(?:TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASSPHRASE|CREDENTIALS?|APIKEY|KEY|AUTH))"
        r"(?P<sep>\s*[=:]\s*)"
        r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
        rf"\g<name>\g<sep>{MASK}",
        re.IGNORECASE,
    ),
    # --password hunter2, --token abc, -p hunter2.
    _rule(
        "sensitive_flag",
        r"(?P<flag>--?(?:password|passwd|token|secret|api[-_]?key|auth)(?:\s+|=))"
        r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
        rf"\g<flag>{MASK}",
        re.IGNORECASE,
    ),
    # Authorization: Bearer …, and the same thing inside a curl -H argument.
    _rule(
        "authorization_header",
        r"(?P<header>(?:proxy-)?authorization\s*:\s*)"
        r"(?P<scheme>bearer|basic|token|digest)?\s*(?P<value>[^\"'\s]+)",
        rf"\g<header>\g<scheme> {MASK}",
        re.IGNORECASE,
    ),
    # curl -u user:pass.
    _rule(
        "basic_auth_flag",
        r"(?P<flag>-u\s+)(?P<user>[^:\s]+):(?P<secret>\S+)",
        rf"\g<flag>\g<user>:{MASK}",
    ),
    # Recognisable token formats, keeping the prefix so the approver can see
    # what kind of credential was in the command.
    # A Telegram bot token lives in the URL path, so it turns up in anything
    # that logs a request line. Keep the numeric bot id, which identifies the
    # bot without granting anything, and drop the half that is the secret.
    _rule("telegram_bot_token", r"(bot\d{5,12}):[A-Za-z0-9_-]{30,}", rf"\g<1>:{MASK}"),
    _rule("github_token", r"\b(gh[pousr]_)[A-Za-z0-9]{16,}", rf"\g<1>{MASK}"),
    _rule("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}", f"sk-{MASK}"),
    _rule("slack_token", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", f"xox-{MASK}"),
    _rule("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b", f"AKIA{MASK}"),
    _rule("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", f"jwt.{MASK}"),
)


@dataclass(frozen=True)
class RedactionResult:
    """Masked text, and which rules had something to do.

    `applied` names rules, never values — it exists so the audit log can say
    "two secrets were masked here" without becoming the place the secrets ended
    up instead.
    """

    text: str
    applied: tuple[str, ...]

    @property
    def redacted(self) -> bool:
        return bool(self.applied)


@dataclass(frozen=True)
class PreparedCommand:
    """A command ready to be shown to a human and stored.

    Both strings are masked. `summary` is what fits on a card; `full` is what
    the approver sees if they ask for it, and what the audit log keeps.
    """

    full: str
    summary: str
    applied: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return self.summary != self.full

    @property
    def redacted(self) -> bool:
        return bool(self.applied)


class Redactor:
    """Applies a list of rules to text. Stateless and safe to share."""

    def __init__(self, rules: tuple[RedactionRule, ...] = DEFAULT_RULES) -> None:
        self._rules = rules

    def redact(self, text: str) -> RedactionResult:
        """Mask every rule that matches, in rule order."""
        applied: list[str] = []
        for rule in self._rules:
            text, count = rule.pattern.subn(rule.replacement, text)
            if count:
                applied.append(rule.name)
        return RedactionResult(text=text, applied=tuple(applied))

    def prepare(self, text: str, *, summary_limit: int = DEFAULT_SUMMARY_LIMIT) -> PreparedCommand:
        """Mask, then shorten — the only order that is safe.

        Shortening first would leak whatever survived the cut, so the two steps
        are not offered separately.
        """
        result = self.redact(text)
        return PreparedCommand(
            full=result.text,
            summary=summarize(result.text, limit=summary_limit),
            applied=result.applied,
        )


class SecretRedactingFilter(logging.Filter):
    """Masks secrets in anything that reaches a log handler.

    Installed on the root logger, so it covers libraries as well as this code.
    That is the point: `TelegramApi.__repr__` was overridden to keep the bot
    token out of tracebacks, and then httpx logged the full request URL at INFO
    — token included, once every poll. Keeping a secret out of *our* log lines
    is not the same as keeping it out of the log.

    Raising the level of the offending logger is the direct fix, and this sits
    underneath it so the next library to do the same thing is already handled.
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or Redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        result = self._redactor.redact(message)
        if result.redacted:
            # Collapsed into msg because the arguments have been folded in
            # already; leaving them would re-interpolate the unmasked values.
            record.msg = result.text
            record.args = ()
        return True


def summarize(text: str, *, limit: int = DEFAULT_SUMMARY_LIMIT) -> str:
    """Collapse whitespace and cut to `limit` characters.

    Public because a card has other long fields to shorten, but note that this
    does no masking at all. For a command, always go through
    `Redactor.prepare()`.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"
