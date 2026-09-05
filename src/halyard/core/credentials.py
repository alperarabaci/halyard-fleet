"""How old the control plane's own credential is getting.

A long-lived token is what keeps remote work going when the desktop login
expires — measured twice, four days apart, each time stopping every delivery
with "OAuth session expired and could not be refreshed" until somebody was back
at the desk. Replacing that with a token fixed it, and moved the problem: the
token expires too, roughly a year on, and it will do so on the same kind of
afternoon.

**Nothing reports when it expires.** `claude auth status` answers with
`loggedIn`, `authMethod`, `apiProvider`, `email`, `orgId`, `orgName`,
`subscriptionType` and `analyticsDisabled` — measured, all eight of them, and
not one is a date. The token itself is opaque. So the expiry cannot be read; it
can only be estimated, and the estimate has to come from somewhere honest.

What is honest is *when Halyard first saw this token*. That is written down the
first time it starts with one, and every warning afterwards says so in those
words rather than pretending to know a deadline. Minting a new one starts a new
clock on its own, because a different token has a different fingerprint.

The token is never stored. What is written is a short SHA-256 of it, which is
enough to tell "still the same one" from "somebody replaced it" and is not
enough to be anything else.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: What `claude setup-token` says it mints: "a token that lasts a year".
ASSUMED_LIFE = timedelta(days=365)

#: How long before that estimate runs out to start saying so.
#:
#: Long enough to act without hurrying — minting a replacement needs a terminal
#: on the machine, so it waits for the next time somebody is at one, and "next
#: month" has to still be true when they get there.
WARN_WITHIN = timedelta(days=30)

#: Enough of the digest to tell one token from another, and no more.
FINGERPRINT = 12


@dataclass(frozen=True)
class Age:
    """What is known about the credential in use."""

    #: When Halyard first started with this token.
    first_seen: datetime
    #: When it is guessed to stop working. An estimate, always.
    expected: datetime

    def days_left(self, now: datetime) -> int:
        return (self.expected - now).days

    def worth_saying(self, now: datetime, within: timedelta = WARN_WITHIN) -> bool:
        return self.expected - now <= within

    def wording(self, now: datetime) -> str:
        """What a person is told, including that it is a guess.

        A date presented as fact would be worse than no warning: somebody would
        trust it, and it is arithmetic on an assumption.
        """
        left = self.days_left(now)
        when = self.first_seen.date().isoformat()
        if left < 0:
            return (
                f"The control plane's token has been in use since {when}, which is over a "
                f"year — it may already have stopped working. Mint a new one at the desk: "
                f"`claude setup-token`."
            )
        return (
            f"The control plane's token has been in use since {when}. Tokens from "
            f"`claude setup-token` last about a year, so this one has roughly {left} days "
            f"left — an estimate, not a deadline anything reports. Mint a replacement at "
            f"the desk when convenient."
        )


def fingerprint(token: str) -> str:
    """A short digest, so a token can be recognised without being kept."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:FINGERPRINT]


def remember(token: str | None, where: Path, *, now: datetime | None = None) -> Age | None:
    """Note this token if it is new, and say how old the one in use is.

    Returns None when there is no token to age — an installation on the desktop
    login has a different problem and is told about that one instead.

    A file that cannot be read or written costs the warning and nothing else.
    This is a note about a credential, not the credential, and no part of the
    gate depends on it.
    """
    if not token:
        return None
    now = now or datetime.now(UTC)
    mark = fingerprint(token)

    seen: dict[str, str] = {}
    try:
        if where.is_file():
            loaded = json.loads(where.read_text(encoding="utf-8"))
            seen = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)

    written = seen.get(mark)
    if written:
        try:
            first = datetime.fromisoformat(written)
        except ValueError:
            first = now
    else:
        first = now
        # Only this token's own line is kept. A record of every credential ever
        # configured is a list nobody wants and nobody asked for.
        try:
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_text(json.dumps({mark: now.isoformat()}), encoding="utf-8")
        except OSError as unwritable:
            logger.warning("Could not note when the token was first seen: %s", unwritable)

    if first.tzinfo is None:
        first = first.replace(tzinfo=UTC)
    return Age(first_seen=first, expected=first + ASSUMED_LIFE)
