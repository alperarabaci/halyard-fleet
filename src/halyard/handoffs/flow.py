"""Making a handoff: its checks first, then the message, then the seat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from halyard import checks, frame
from halyard.core.config_file import Handoff
from halyard.handoffs.message import compose
from halyard.handoffs.spec import Delivery, Handed

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
    project_checks: Mapping[str, Path],
    asker: checks.Asker | None,
    model: str,
    timeout: float,
    delivery: Delivery,
) -> Handed:
    """Run the checks a handoff names over the reply, write the message, deliver it.

    The checks run side by side, each a turn of its own, and all before the
    message is written: the seat receives the reply and what the checks made of
    it together. A check that could not run goes as unmeasured rather than
    being left out — the project's prompts say an unmeasured line is not a
    clean one, and the reader has to see it to know that.
    """
    answers: tuple[checks.Answer, ...] = ()
    if handoff.checks and (asker is None or reply is None):
        why = "no runtime here can take a one-shot turn" if asker is None else "there was no reply"
        answers = tuple(
            checks.Answer(name, project_checks[name], "not run", why=why) for name in handoff.checks
        )
    elif handoff.checks:
        answers = tuple(
            await asyncio.gather(
                *(
                    checks.run(
                        name,
                        project_checks[name],
                        project=project,
                        context=context,
                        note=note,
                        reply=reply,
                        asker=asker,
                        model=model,
                        timeout=timeout,
                        about=f"handoff {handoff.name}, {sender}'s reply from {arrived}",
                    )
                    for name in handoff.checks
                )
            )
        )

    prompt, prompt_ref = "", ""
    if handoff.prompt:
        prompt = frame.read(handoff.prompt, project)
        revision = await asyncio.to_thread(frame.version, handoff.prompt, project)
        prompt_ref = f"{handoff.prompt} @ {revision}" + ("" if prompt else " — could not be read")

    text = compose(
        handoff.name,
        recipient=recipient,
        sender=sender,
        arrived=arrived,
        context=context,
        note=note,
        prompt=prompt,
        prompt_ref=prompt_ref,
        answers=answers,
        reply=reply,
    )
    await delivery.to_seat(recipient_label, text)
    logger.info(
        "Handoff %s: %s → %s, %d chars, checks: %s",
        handoff.name,
        sender,
        recipient,
        len(text),
        ", ".join(f"{a.name} {'answered' if a.measured else 'unmeasured'}" for a in answers)
        or "none",
    )
    return Handed(text, answers)
