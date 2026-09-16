"""What a seat decided, read off the end of its reply.

A reply that decides ends with one word, alone on its last line and usually
after a label — `DECISION: forward` — because somebody reading on a phone needs
the answer before the reasoning. A workflow reads that line and nothing else:
the same word in the middle of a paragraph is prose, and a flow that moved on
prose would move on a sentence about what somebody was thinking of doing.

**The words are `forward`, `back` and `wait` unless a project says otherwise**
under `workflows: decisions:` — the ones its prompts ask for, in whatever
language those are written in. The label is the project's own too: whatever
comes before the last colon is not read, so `DECISION: forward`,
`RESULT: forward` and a line that is only `forward` all say the same.

**A reply that decides nothing carries on.** Not every handoff asks for a
decision, and a flow that stopped whenever a seat answered in prose would stop
constantly. The chat is told which it was, so a step taken on no decision is
taken in the open.
"""

from __future__ import annotations

from enum import StrEnum

from halyard.core.config_file import Decisions


class Decision(StrEnum):
    """What the last line of a reply said to do."""

    FORWARD = "forward"
    BACK = "back"
    WAIT = "wait"


#: What a model wraps a line in when it wants it seen — `**DECISION: back**` —
#: which says the same thing as the line without it.
_DRESSING = " \t*_`>#-."


def read(reply: str | None, words: Decisions | None = None) -> Decision | None:
    """The decision on the last line of `reply`, or None when it says none."""
    if not reply:
        return None
    last = next((line for line in reversed(reply.splitlines()) if line.strip()), "")
    said = last.rpartition(":")[2].strip(_DRESSING).casefold()
    if not said:
        return None
    words = words or Decisions()
    for decision in Decision:
        if said == word_for(decision, words).strip(_DRESSING).casefold():
            return decision
    return None


def word_for(decision: Decision, words: Decisions | None = None) -> str:
    """The word a project decides `decision` in — its own name unless it said otherwise."""
    return getattr(words or Decisions(), decision.value)


def carried(value: str) -> Decision | None:
    """A decision as a run keeps it, or None for one it does not recognise."""
    try:
        return Decision(value) if value else None
    except ValueError:
        return None
