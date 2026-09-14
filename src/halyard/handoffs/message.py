"""The message a seat receives when something is handed to it."""

from __future__ import annotations

from collections.abc import Sequence

from halyard import frame
from halyard.checks import Answer
from halyard.commands import Command, Result


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
) -> str:
    """Everything a seat needs to act on a handoff, in the order it reads it.

    Who it is for and from, first, so a session can tell a handoff from an
    instruction typed at it. Then the project's own text for this handoff, which
    speaks of "the message below". Then the envelope: what Halyard can see for
    itself — the task, the machine, the tree, which revision of the prompt — one
    fact to a line, because the project's prompts refuse to guess at any of it.
    Then what its commands printed, and the checks' answers, measured or not.
    Then the reply itself, last, where the prompt said it would be.
    """
    parts = [f"To {recipient}, from Halyard — handoff: {name}."]
    if prompt:
        parts += ["", prompt]
    facts = list(context)
    if prompt_ref:
        facts.append(f"Prompt: {prompt_ref}")
    if note:
        facts.append(f"Said by whoever asked: {note}")
    if reply is not None:
        facts.append(f"From: {sender}, reply from {arrived} ({len(reply):,} characters)")
    parts += ["", "---", "", *frame.envelope(facts)]
    for command, result in ran:
        parts += ["", f"Command {command.name} — {command.line}:", ""]
        parts.append(result.output or "(it printed nothing)")
    for answer in answers:
        parts += ["", f"Check {answer.name} — {answer.path} @ {answer.version}:", ""]
        parts.append(answer.text if answer.measured else f"unmeasured — {answer.why}")
    if reply is not None:
        parts += ["", f"{sender}'s reply:", "", reply]
    return "\n".join(parts)
