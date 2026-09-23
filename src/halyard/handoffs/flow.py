"""Making a handoff: its commands, then its inspections, then the message, then the seat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from halyard import frame, inspections
from halyard.commands import Command, Result, summary
from halyard.core.config_file import Handoff
from halyard.handoffs import rounds
from halyard.handoffs.message import compose
from halyard.handoffs.spec import Delivery, Handed, Previous, Runner

logger = logging.getLogger(__name__)


async def hand_off(
    handoff: Handoff,
    *,
    project: Path,
    context: list[str],
    note: str,
    reply: str | None,
    arrived: str,
    sender: str,
    recipient_label: str,
    recipient: str,
    project_inspections: Mapping[str, Path],
    asker: inspections.Asker | None,
    model: str,
    timeout: float,
    delivery: Delivery,
    findings: Sequence[str] = (),
    labeller: inspections.Labeller | None = None,
    project_commands: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    round_number: int | None = None,
    expected: int | None = None,
    previous: Previous | None = None,
    keeper: inspections.Keeper | None = None,
) -> Handed:
    """Run a handoff's commands, then its inspections, write the message, deliver it.

    The one place the parts of a handoff meet, in this order, each handed what
    came before it. The commands run first, one after another, where the
    project is; what each did becomes a line of the envelope, so the inspections read
    Halyard's own run rather than setting out to make one. A command that fails
    is reported and the handoff goes on — whoever receives it has to see that it
    failed.

    The inspections run side by side, each a turn of its own, and all before
    the message is written: the seat receives the reply and what they made of
    it together. An inspection that could not run goes as unmeasured rather than
    being left out — the project's prompts say an unmeasured line is not a
    clean one, and the reader has to see it to know that. An inspection that finds
    something labels the task as it would run by hand: the project's `findings`
    decide, not the handoff.

    `round_number` is which round of a workflow's step this is, this one
    included, and None for a handoff pressed by hand, which counts nothing —
    see `halyard.handoffs.rounds`. From the second round on, a handoff with a
    `followup_prompt:` sends that in place of its `prompt:`, and `previous`
    carries what the seat said back to the round before. `expected` is how many
    rounds the step allows. `keeper` keeps each inspection it runs — see
    `halyard.inspections.record`.
    """
    ran: list[tuple[Command, Result]] = []
    for name in handoff.commands:
        command = Command(name=name, line=(project_commands or {}).get(name, ""))
        if runner is None:
            result = Result(ok=False, output="nothing here can run a command", seconds=0.0)
        else:
            try:
                result = await runner.run(command)
            except Exception:
                logger.warning("Handoff %s could not run %s", handoff.name, name, exc_info=True)
                result = Result(ok=False, output="it could not be run", seconds=0.0)
        ran.append((command, result))
    envelope = [
        *context,
        *([f"Round: {rounds.shown(round_number, expected)}"] if round_number else []),
        *(summary(c.name, c.line, r) for c, r in ran),
    ]

    answers: tuple[inspections.Answer, ...] = ()
    if handoff.inspections and (asker is None or reply is None):
        why = "no runtime here can take a one-shot turn" if asker is None else "there was no reply"
        answers = tuple(
            inspections.Answer(name, project_inspections[name], "not run", why=why)
            for name in handoff.inspections
        )
    elif handoff.inspections:
        answers = tuple(
            await asyncio.gather(
                *(
                    inspections.run(
                        name,
                        project_inspections[name],
                        project=project,
                        context=envelope,
                        note=note,
                        reply=reply,
                        asker=asker,
                        model=model,
                        timeout=timeout,
                        about=f"handoff {handoff.name}, {sender}'s reply from {arrived}",
                        handoff=handoff.name,
                        findings=findings,
                        labeller=labeller,
                        keeper=keeper,
                    )
                    for name in handoff.inspections
                )
            )
        )

    written = handoff.prompt
    if round_number is not None and round_number > 1 and handoff.followup_prompt:
        written = handoff.followup_prompt
    prompt, prompt_ref = "", ""
    if written:
        prompt = frame.read(written, project)
        revision = await asyncio.to_thread(frame.version, written, project)
        prompt_ref = f"{written} @ {revision}" + ("" if prompt else " — could not be read")

    text = compose(
        handoff.name,
        recipient=recipient,
        sender=sender,
        arrived=arrived,
        context=envelope,
        note=note,
        prompt=prompt,
        prompt_ref=prompt_ref,
        answers=answers,
        reply=reply,
        ran=ran,
        previous=previous,
    )
    await delivery.to_seat(recipient_label, text)
    logger.info(
        "Handoff %s: %s → %s, %d chars, inspections: %s · %s",
        handoff.name,
        sender,
        recipient,
        len(text),
        ", ".join(f"{a.name} {'answered' if a.measured else 'unmeasured'}" for a in answers)
        or "none",
        " · ".join(envelope),
    )
    return Handed(text, answers, tuple(ran))
