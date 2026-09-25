"""The message a seat receives when something is handed to it."""

from __future__ import annotations

from collections.abc import Sequence

from halyard import frame
from halyard.commands import Command, Result
from halyard.inspections import Answer
from halyard.transitions.spec import Previous


def compose(
    name: str,
    *,
    recipient: str,
    sender: str,
    arrived: str,
    context: list[str],
    note: str,
    prompt: str,
    prompt_ref: str,
    answers: Sequence[Answer],
    reply: str | None,
    ran: Sequence[tuple[Command, Result]] = (),
    previous: Previous | None = None,
) -> str:
    """Everything a seat needs to act on a transition, in the order it reads it.

    Who it is for and from, first, so a session can tell a transition from an
    instruction typed at it. Then the project's own text for this transition, which
    speaks of "the message below". Then the envelope: what Halyard can see for
    itself — the task, the machine, the tree, which round this is and which
    revision of the prompt — one fact to a line, because the project's prompts
    refuse to guess at any of it. Then what its commands printed, and the
    inspections' answers, measured or not. Then, on a round after the first, what the
    seat said back to the round before. Then the reply itself, last, where the
    prompt said it would be.
    """
    parts = [f"To {recipient}, from Halyard — transition: {name}."]
    if prompt:
        parts += ["", prompt]
    facts = list(context)
    if prompt_ref:
        facts.append(f"Prompt: {prompt_ref}")
    if note:
        facts.append(f"Said by whoever asked: {note}")
    if reply is not None:
        facts.append(f"From: {sender}, reply from {arrived} ({len(reply):,} characters)")
    if previous is not None:
        facts.append(_answered(previous))
    parts += ["", "---", "", *frame.envelope(facts)]
    for command, result in ran:
        parts += ["", f"Command {command.name} — {command.line}:", ""]
        parts.append(result.output or "(it printed nothing)")
    for answer in answers:
        parts += ["", f"Inspection {answer.name} — {answer.path} @ {answer.version}:", ""]
        parts.append(answer.text if answer.measured else f"unmeasured — {answer.why}")
    if previous is not None and previous.text is not None:
        parts += ["", f"{previous.seat}'s answer to round {previous.number}:", "", previous.text]
    if reply is not None:
        parts += ["", f"{sender}'s reply:", "", reply]
    return "\n".join(parts)


def _answered(previous: Previous) -> str:
    """The envelope's line for what the seat said to the round before."""
    if previous.text is None:
        return (
            f"Previous answer: none — {previous.seat} has said nothing since round "
            f"{previous.number} reached it at {previous.sent}"
        )
    return (
        f"Previous answer: {previous.seat}, from {previous.at}, to round {previous.number} "
        f"({len(previous.text):,} characters)"
    )
