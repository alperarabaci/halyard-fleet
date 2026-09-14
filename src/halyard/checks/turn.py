"""One check, run as a model turn of its own over a reply."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from halyard import frame
from halyard.checks.spec import Answer, Asker, StoppedError

logger = logging.getLogger(__name__)


def prompt(instructions: str, *, context: list[str], note: str, text: str) -> str:
    """One check's turn: its own instructions, what Halyard knows, and the text."""
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
    """What a seat is handed when somebody sends it a check's answer.

    Written to be read cold by a session that saw none of it happen: what ran,
    on whose reply, where, what it found — and the reply itself, because a
    finding about a report the reader does not have is a finding nobody can
    weigh. Measured: a navigator handed the findings alone could not tell what
    they were about.
    """
    return "\n".join(
        [
            f"The operator ran this project's `{name}` check on {author}'s reply "
            f"from {arrived} and is handing you the result: what the check found, "
            "then the reply it checked.",
            "",
            f"Check: {name} — {path} @ {version}",
            "",
            *frame.envelope(context),
            "",
            "What it found:",
            "",
            findings,
            "",
            f"The reply it checked ({len(reply):,} characters):",
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
) -> Answer:
    """Run one check over a reply: its own text, what Halyard can see, the reply.

    The turn stands in the project, because a report is compared against the
    code it is about: it may read, and run what the check's text sends it to,
    and edits nothing. It goes by the check's name — and the handoff's, when it
    runs for one — so that a command it wants run reaches a person as that
    check's rather than a stranger's.

    Never raises. A check that cannot be read, or a model that does not answer,
    comes back as an `Answer` with no text and the reason — never as nothing,
    because a missing line reads exactly like a clean one.

    Logged as a frame rather than the files — enough to say afterwards what a
    finding was about and which revision of the check found it — and then the
    answer as it came.
    """
    instructions = frame.read(path, project)
    version = await asyncio.to_thread(frame.version, path, project)
    if not instructions:
        logger.warning("Check %s did not run: could not read %s", name, path)
        return Answer(name, path, version, why=f"could not read {path}")
    logger.info(
        "Check %s asked · %s · %s · check %s @ %s, %d chars · note %r · model %s",
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
        logger.info("Check %s %s after %.1fs", name, stopped, took)
        return Answer(name, path, version, why=str(stopped), took=took)
    except Exception:
        logger.warning("Check %s failed", name, exc_info=True)
        said = None
    took = time.monotonic() - started
    if not said:
        logger.info("Check %s got no answer in %.1fs", name, took)
        return Answer(name, path, version, why="the model did not answer", took=took)
    logger.info("Check %s answered in %.1fs:\n%s", name, took, said)
    return Answer(name, path, version, text=unfenced(said), took=took)
