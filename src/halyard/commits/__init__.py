"""Committing a branch's work from a phone.

Its own package, deliberately. This shares nothing with the runtime machinery
next door: there is no `RuntimeSpec` here, no session, no seat, no transcript.
A commit is a fact about a *repository*, and the only thing it borrows from the
rest of Halyard is a way to ask a model for one sentence — which arrives as a
callable, so nothing in here has to know that Claude Code exists.

Three pieces, each testable without the other two:

- `repository` — what git says and what git is told. Pure subprocess work.
- `proposals` — commits offered and not yet answered, and when they go stale.
- The channel renders them. `channels/telegram/commit_card.py` is that, kept
  apart from the approval cards for the same reason this package exists.
"""

from halyard.commits.proposals import Proposal, Proposals
from halyard.commits.repository import (
    Change,
    GitError,
    Uncommitted,
    assemble,
    commit,
    prompt,
    push,
    read,
    reference_for,
    summary_of,
)

__all__ = [
    "Change",
    "GitError",
    "Proposal",
    "Proposals",
    "Uncommitted",
    "assemble",
    "commit",
    "prompt",
    "push",
    "read",
    "reference_for",
    "summary_of",
]
