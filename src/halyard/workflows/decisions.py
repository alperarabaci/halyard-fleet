"""What a seat decided, read off the end of its reply.

A reply that decides ends with one line, alone: `DECISION: forward`,
`DECISION: back` or `DECISION: wait`, because somebody reading on a phone needs
the answer before the reasoning. A workflow reads that line and nothing else:
the same word in the middle of a paragraph is prose, and a flow that moved on
prose would move on a sentence about what somebody was thinking of doing.

**The words are fixed.** Every step's envelope says which word takes the work
where, so a project's prompts only have to ask for the line — and one set of
words is one thing to get right, where words each project configured were a
second place to keep in step with its prompts.

**A reply that decides nothing carries on.** Not every handoff asks for a
decision, and a flow that stopped whenever a seat answered in prose would stop
constantly. The chat is told which it was, so a step taken on no decision is
taken in the open.
"""

from __future__ import annotations

from enum import StrEnum


class Decision(StrEnum):
    """What the last line of a reply said to do."""

    FORWARD = "forward"
    BACK = "back"
    WAIT = "wait"


#: What the decision line starts with.
LABEL = "DECISION"

#: What a model wraps a line in when it wants it seen — `**DECISION: back**` —
#: which says the same thing as the line without it.
_DRESSING = " \t*_`>#-."


def read(reply: str | None) -> Decision | None:
    """The decision on the last line of `reply`, or None when it says none."""
    if not reply:
        return None
    last = next((line for line in reversed(reply.splitlines()) if line.strip()), "")
    label, colon, word = last.rpartition(":")
    if colon and label.strip(_DRESSING).casefold() != LABEL.casefold():
        # Some other label's value — `Status: back` — is not a decision.
        return None
    # A line that is only the word answers the same way as the labelled one.
    said = (word if colon else last).strip(_DRESSING).casefold()
    try:
        return Decision(said)
    except ValueError:
        return None


def carried(value: str) -> Decision | None:
    """A decision as a run keeps it, or None for one it does not recognise."""
    try:
        return Decision(value) if value else None
    except ValueError:
        return None
