"""Taking a transition: its commands, then its inspections, then the message, then the seat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from halyard import frame, inspections
from halyard.commands import NOT_RUN, Command, Result, outcome, summary
from halyard.core.config_file import ModelChoice, Transition
from halyard.transitions import rounds
from halyard.transitions.message import compose
from halyard.transitions.spec import Delivery, Handed, Previous, Runner

logger = logging.getLogger(__name__)


async def take(
    transition: Transition,
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
    effort: str | None = None,
    models: Mapping[str, ModelChoice] | None = None,
    project_commands: Mapping[str, str] | None = None,
    project_not_run: Mapping[str, Sequence[int]] | None = None,
    runner: Runner | None = None,
    round_number: int | None = None,
    expected: int | None = None,
    previous: Previous | None = None,
    keeper: inspections.Keeper | None = None,
    step: str = "",
) -> Handed:
    """Run a transition's commands, then its inspections, write the message, deliver it.

    The one place the parts of a transition meet, in this order, each handed what
    came before it. The commands run first, one after another, where the
    project is; what each did becomes a line of the envelope, so the inspections read
    Halyard's own run rather than setting out to make one. A command that fails
    is reported and the transition goes on — whoever receives it has to see that it
    failed.

    A command that did not run — no result at all, see `running.outcome` — stops
    the transition there: the commands after it, the inspections and the
    delivery are all skipped, and `Handed.stopped` says why. Inspections over
    a run that never happened would read green in nothing.

    The inspections run side by side, each a turn of its own, and all before
    the message is written: the seat receives the reply and what they made of
    it together. An inspection that could not run goes as unmeasured rather than
    being left out — the project's prompts say an unmeasured line is not a
    clean one, and the reader has to see it to know that.

    `round_number` is which round of a workflow's step this is, this one
    included, and None for a transition pressed by hand, which counts nothing —
    see `halyard.transitions.rounds`. From the second round on, a transition with a
    `followup_prompt:` sends that in place of its `prompt:`, and `previous`
    carries what the seat said back to the round before. `expected` is how many
    rounds the step allows. `keeper` keeps each inspection it runs — see
    `halyard.inspections.record`. `step` names the workflow's step this is, for
    the message's first line, and is empty for a transition pressed by hand.

    Each inspection runs on `model` at `effort`, unless `models` — the
    project's own, by inspection — names another for it.
    """
    ran, stopped = await run_commands(
        transition.commands,
        lines=project_commands or {},
        not_run=project_not_run or {},
        runner=runner,
        about=f"Transition {transition.name}",
    )
    if stopped is not None:
        return Handed("", (), ran, stopped=stopped)
    envelope = [
        *context,
        *([f"Round: {rounds.shown(round_number, expected)}"] if round_number else []),
        *(summary(c.name, c.line, r, c.not_run) for c, r in ran),
    ]

    answers = await run_inspections(
        transition.inspections,
        project_inspections=project_inspections,
        project=project,
        context=envelope,
        note=note,
        reply=reply,
        asker=asker,
        model=model,
        effort=effort,
        models=models,
        timeout=timeout,
        about=f"transition {transition.name}, {sender}'s reply from {arrived}",
        transition=transition.name,
        keeper=keeper,
    )

    written = transition.prompt
    if round_number is not None and round_number > 1 and transition.followup_prompt:
        written = transition.followup_prompt
    prompt, prompt_ref = "", ""
    if written:
        prompt = frame.read(written, project)
        revision = await asyncio.to_thread(frame.version, written, project)
        prompt_ref = f"{written} @ {revision}" + ("" if prompt else " — could not be read")

    text = compose(
        transition.name,
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
        step=step,
    )
    await delivery.to_seat(recipient_label, text)
    logger.info(
        "Transition %s: %s → %s, %d chars, inspections: %s · %s",
        transition.name,
        sender,
        recipient,
        len(text),
        ", ".join(f"{a.name} {'answered' if a.measured else 'unmeasured'}" for a in answers)
        or "none",
        " · ".join(envelope),
    )
    return Handed(text, answers, tuple(ran))


async def run_commands(
    names: Sequence[str],
    *,
    lines: Mapping[str, str],
    not_run: Mapping[str, Sequence[int]],
    runner: Runner | None,
    about: str,
    stop_at: Collection[str] = (NOT_RUN,),
) -> tuple[tuple[tuple[Command, Result], ...], str | None]:
    """Run commands one after another, where the project is, through `runner`.

    What each did, and — when one ended in `stop_at` — the line saying what it
    did, after which nothing more runs; None when they all ran. A transition
    stops only at a command that did not run: one that failed is a result its
    seat has to see. A commit option stops at a failure too, since a failed
    check is a fact with nothing in it to weigh. `about` says whose commands
    these are, in the log.
    """
    ran: list[tuple[Command, Result]] = []
    for name in names:
        command = Command(name=name, line=lines.get(name, ""), not_run=tuple(not_run.get(name, ())))
        if runner is None:
            result = Result(ok=False, output="nothing here can run a command", seconds=0.0)
        else:
            try:
                result = await runner.run(command)
            except Exception:
                logger.warning("%s could not run %s", about, name, exc_info=True)
                result = Result(ok=False, output="it could not be run", seconds=0.0)
        ran.append((command, result))
        ended = outcome(result, command.not_run)
        if ended in stop_at:
            last = result.output.splitlines()[-1] if result.output else ""
            logger.warning(
                "%s %s · exit %s · %.0fs · %s",
                ended.capitalize(),
                name,
                result.exit_code,
                result.seconds,
                last or "(nothing printed)",
            )
            return tuple(ran), summary(name, command.line, result, command.not_run)
    return tuple(ran), None


async def run_inspections(
    names: Sequence[str],
    *,
    project_inspections: Mapping[str, Path],
    project: Path,
    context: list[str],
    note: str,
    reply: str | None,
    asker: inspections.Asker | None,
    model: str,
    timeout: float,
    about: str,
    effort: str | None = None,
    models: Mapping[str, ModelChoice] | None = None,
    transition: str = "",
    keeper: inspections.Keeper | None = None,
) -> tuple[inspections.Answer, ...]:
    """Run inspections side by side over `reply`, each a turn of its own.

    Each on `model` at `effort`, unless `models` — the project's own, by
    inspection — names another for it. With nothing to take a turn, or nothing
    to read, each comes back not run, saying why, rather than being left out:
    a missing answer reads exactly like a clean one.
    """
    if not names:
        return ()
    if asker is None or reply is None:
        why = "no runtime here can take a one-shot turn" if asker is None else "there was no reply"
        return tuple(
            inspections.Answer(name, project_inspections[name], "not run", why=why)
            for name in names
        )
    broader = ModelChoice(model, effort)
    chosen = {name: (models or {}).get(name, ModelChoice()).over(broader) for name in names}
    return tuple(
        await asyncio.gather(
            *(
                inspections.run(
                    name,
                    project_inspections[name],
                    project=project,
                    context=context,
                    note=note,
                    reply=reply,
                    asker=asker,
                    model=chosen[name].model or model,
                    effort=chosen[name].effort,
                    timeout=timeout,
                    about=about,
                    transition=transition,
                    keeper=keeper,
                )
                for name in names
            )
        )
    )
