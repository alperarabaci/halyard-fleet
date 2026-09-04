"""Commits offered and not yet answered.

Small enough to look trivial, and it is not: three of the four ways this
feature could commit something nobody agreed to are decided here.

**Taken, not read.** `take` removes the proposal as it hands it over, and that
is the only thing stopping a double tap from making two commits. There is no
nonce — what this answers is not a request any store knows about — so the
removal *is* the guard.

**Kept for a rewrite.** Asking for new wording must not consume the proposal;
the sentence still has to find it when it arrives.

**Dropped when stale.** The card describes a working tree at one moment. A
button tapped tomorrow would commit whatever the branch holds then, under a
message written for something else.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from halyard.commits.repository import Uncommitted

#: How long a proposed commit is worth answering. Long enough to read a file
#: list and think, short enough that the tree it describes is still that tree.
PROPOSAL_SECONDS = 900


@dataclass(frozen=True)
class Proposal:
    """A commit waiting for somebody to say yes.

    Holds what the card showed. Re-reading the repository on the button press
    would risk committing something never displayed — the message would be
    describing one set of changes and the commit would contain another.
    """

    project: str
    path: Path
    work: Uncommitted
    message: str
    at: datetime
    #: What the model said actually changed, for the card. Never committed —
    #: see `repository.summary_of` for why a body nobody's history has ever
    #: carried does not get invented here.
    summary: tuple[str, ...] = ()
    #: Whether this card offers the project's review round. Held rather than
    #: recomputed, because it is a fact about which command made the card:
    #: rewording a plain `/commit` must not grow a button it never had.
    reviewed: bool = False
    #: What was worth flagging but not worth refusing over. Held so rewording
    #: the message redraws the card with the warning still on it — a warning
    #: that disappears when you type is a warning nobody heeds twice.
    warnings: tuple[str, ...] = ()


class Proposals:
    """The open proposals, by the handle their buttons carry."""

    def __init__(self, clock: Callable[[], datetime], *, ttl: int = PROPOSAL_SECONDS) -> None:
        self._clock = clock
        self._ttl = timedelta(seconds=ttl)
        self._open: dict[str, Proposal] = {}

    def __len__(self) -> int:
        return len(self._open)

    def __contains__(self, handle: object) -> bool:
        return self.peek(str(handle)) is not None

    def add(
        self,
        project: str,
        path: Path,
        work: Uncommitted,
        message: str,
        summary: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
        reviewed: bool = False,
    ) -> str:
        """Offer one, and return the handle its buttons will carry."""
        self._forget_stale()
        handle = secrets.token_hex(4)
        self._open[handle] = Proposal(
            project=project,
            path=path,
            work=work,
            message=message,
            at=self._clock(),
            summary=summary,
            warnings=warnings,
            reviewed=reviewed,
        )
        return handle

    def peek(self, handle: str) -> Proposal | None:
        """The proposal, left in place. For a rewrite, which is not an answer."""
        found = self._open.get(handle)
        if found is None:
            return None
        if self._clock() - found.at > self._ttl:
            del self._open[handle]
            return None
        return found

    def take(self, handle: str) -> Proposal | None:
        """The proposal, removed. For an answer, which happens once."""
        found = self.peek(handle)
        if found is not None:
            del self._open[handle]
        return found

    def reword(self, handle: str, message: str) -> Proposal | None:
        """Replace the wording, and start its clock again.

        Restarted on purpose: somebody who just typed a sentence is about to
        press a button, and expiring underneath them would be absurd.
        """
        found = self.peek(handle)
        if found is None:
            return None
        self._open[handle] = replace(found, message=message, at=self._clock())
        return self._open[handle]

    def _forget_stale(self) -> None:
        cutoff = self._clock() - self._ttl
        for handle, proposal in list(self._open.items()):
            if proposal.at < cutoff:
                del self._open[handle]
