"""Making a handoff: its commands, then its checks, then the message, then the seat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from halyard import checks, frame
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
    project_checks: Mapping[str, Path],
    asker: checks.Asker | None,
    model: str,
    timeout: float,
    delivery: Delivery,
    findings: Sequence[str] = (),
    labeller: checks.Labeller | None = None,
    project_commands: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    round_number: int | None = None,
    expected: int = rounds.EXPECTED,
    previous: Previous | None = None,
) -> Handed:
    """Run a handoff's commands, then its checks, write the message, deliver it.

    The one place the parts of a handoff meet, in this order, each handed what
    came before it. The commands run first, one after another, where the
    project is; what each did becomes a line of the envelope, so the checks read
    Halyard's own run rather than setting out to make one. A command that fails
    is reported and the handoff goes on — whoever receives it has to see that it
    failed.

    The checks run side by side, each a turn of its own, and all before the
    message is written: the seat receives the reply and what the checks made of
    it together. A check that could not run goes as unmeasured rather than
    being left out — the project's prompts say an unmeasured line is not a
    clean one, and the reader has to see it to know that. A check that finds
    something labels the task as it would run by hand: the project's `findings`
    decide, not the handoff.

    `round_number` is which time this handoff goes for its work item, this one
    included, and None where nothing is counted — see `halyard.handoffs.rounds`.
    From the second round on, a handoff with a `followup_prompt:` sends that in
    place of its `prompt:`, and `previous` carries what the seat said back to
    the round before. `expected` is how many rounds this one is counted against,
    which a workflow's step decides for the steps it takes.
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
                    )
                    for name in handoff.checks
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
        "Handoff %s: %s → %s, %d chars, checks: %s · %s",
        handoff.name,
        sender,
        recipient,
        len(text),
        ", ".join(f"{a.name} {'answered' if a.measured else 'unmeasured'}" for a in answers)
        or "none",
        " · ".join(envelope),
    )
    return Handed(text, answers, tuple(ran))
