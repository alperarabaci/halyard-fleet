"""One inspection, run as a model turn of its own over a reply."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from halyard import frame
from halyard.inspections.spec import Answer, Asker, Labeller, StoppedError

logger = logging.getLogger(__name__)

#: What a finding puts on the task: Halyard's corner of the tracker, then the
#: inspection's name — `halyard:claims`. One colon, not a scope; see
#: `halyard.tasks.attribution` for why.
LABEL_PREFIX = "halyard:"


def prompt(instructions: str, *, context: list[str], note: str, text: str) -> str:
    """One inspection's turn: its own instructions, what Halyard knows, and the text.

    It still says "check" to the model, as it did before these were called
    inspections: what a model is told stays the same, so that answers from
    before the name changed and after it can be compared.
    """
    parts = [
        instructions,
        "",
        "---",
        "",
        "This check runs in the project's own directory. It can read files and "
        "cannot edit anything.",
        "",
        "It runs only the commands this check's text tells it to, each once and "
        "as written — nothing of its own: no probes, no reproductions, no scratch "
        "copies, no setup or cleanup. Every command waits for the operator to "
        "allow it, and one that is refused or does not finish counts as "
        "unmeasured.",
        "",
        "It is checking, not changing: read what this check needs. The project's "
        "own instructions, and the files they point to, are background here "
        "rather than a reading list.",
        "",
        *frame.envelope([*context, *([f"Said by whoever asked: {note}"] if note else [])]),
        "",
        "Text to check:",
        "",
        text,
    ]
    return "\n".join(parts)


def unfenced(answer: str) -> str:
    """An answer wrapped whole in a code fence, without the fence.

    It is shown in `<pre>` already, where a fence would print as backticks.
    """
    lines = answer.strip().splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return answer.strip()


def finding(answer: str, findings: Sequence[str]) -> str | None:
    """Which of the project's finding phrases an answer says, if any.

    In the project's own words, as its inspection files have the model write them —
    `status: candidate` — matched regardless of case; the first that matches. No
    phrases, no finding: a project that has not said what a finding looks like
    is never guessed for.
    """
    said = answer.casefold()
    return next(
        (phrase for phrase in findings if phrase.strip() and phrase.casefold() in said), None
    )


def handed_on(
    name: str,
    *,
    path: Path,
    version: str,
    author: str,
    arrived: str,
    context: list[str],
    findings: str,
    reply: str,
) -> str:
    """What a seat is handed when somebody sends it an inspection's answer.

    Written to be read cold by a session that saw none of it happen: what ran,
    on whose reply, where, what it found — and the reply itself, because a
    finding about a report the reader does not have is a finding nobody can
    weigh. Measured: a navigator handed the findings alone could not tell what
    they were about.
    """
    return "\n".join(
        [
            f"The operator ran this project's `{name}` inspection on {author}'s reply "
            f"from {arrived} and is handing you the result: what the inspection "
            "found, then the reply it inspected.",
            "",
            f"Inspection: {name} — {path} @ {version}",
            "",
            *frame.envelope(context),
            "",
            "What it found:",
            "",
            findings,
            "",
            f"The reply it inspected ({len(reply):,} characters):",
            "",
            reply,
        ]
    )


async def run(
    name: str,
    path: Path,
    *,
    project: Path,
    context: list[str],
    note: str,
    reply: str,
    asker: Asker,
    model: str,
    timeout: float,
    about: str = "",
    handoff: str = "",
    findings: Sequence[str] = (),
    labeller: Labeller | None = None,
) -> Answer:
    """Run one inspection over a reply: its own text, what Halyard can see, the reply.

    The turn stands in the project, because a report is compared against the
    code it is about: it may read, and run what the inspection's text sends it
    to, and edits nothing. It goes by the inspection's name — and the handoff's,
    when it runs for one — so that a command it wants run reaches a person as
    that inspection's rather than a stranger's.

    An answer that says one of the project's `findings` puts the inspection's
    label on the task through `labeller`. Decided here, from the project's own
    words, so whoever runs it has no say in it.

    Never raises. An inspection that cannot be read, or a model that does not answer,
    comes back as an `Answer` with no text and the reason — never as nothing,
    because a missing line reads exactly like a clean one.

    Logged as a frame rather than the files — enough to say afterwards what a
    finding was about and which revision of the inspection found it — and then the
    answer as it came.
    """
    instructions = frame.read(path, project)
    version = await asyncio.to_thread(frame.version, path, project)
    if not instructions:
        logger.warning("Inspection %s did not run: could not read %s", name, path)
        return Answer(name, path, version, why=f"could not read {path}")
    logger.info(
        "Inspection %s asked · %s · %s · file %s @ %s, %d chars · note %r · model %s",
        name,
        " · ".join(context),
        about or f"reply {len(reply)} chars",
        path,
        version,
        len(instructions),
        note,
        model,
    )
    started = time.monotonic()
    try:
        said = await asker.ask(
            prompt(instructions, context=context, note=note, text=reply),
            model=model,
            timeout=timeout,
            cwd=project,
            name=f"{name} · handoff {handoff}" if handoff else name,
            edits=False,
        )
    except StoppedError as stopped:
        took = time.monotonic() - started
        logger.info("Inspection %s %s after %.1fs", name, stopped, took)
        return Answer(name, path, version, why=str(stopped), took=took)
    except Exception:
        logger.warning("Inspection %s failed", name, exc_info=True)
        said = None
    took = time.monotonic() - started
    if not said:
        logger.info("Inspection %s got no answer in %.1fs", name, took)
        return Answer(name, path, version, why="the model did not answer", took=took)
    logger.info("Inspection %s answered in %.1fs:\n%s", name, took, said)
    if labeller is not None and (phrase := finding(said, findings)):
        await _label(name, phrase, labeller)
    return Answer(name, path, version, text=unfenced(said), took=took)


async def _label(name: str, phrase: str, labeller: Labeller) -> None:
    """Put the inspection's label on the task. Never raises: a label that could not
    be written is a gap in a record, and the answer is worth more."""
    label = f"{LABEL_PREFIX}{name}"
    logger.info("Inspection %s found something (%r); %s goes on the task", name, phrase, label)
    try:
        await labeller.label(label)
    except Exception:
        logger.warning("Inspection %s could not put %s on the task", name, label, exc_info=True)
