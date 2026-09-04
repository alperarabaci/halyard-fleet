"""The round some work needs before it is committed.

Not everything is catchable by a guard. A test proves what it tests, a lint rule
proves what it matches, and a file of invariants proves nothing at all — an
agent's attention is finite, and past a point more written rules are noise
competing with the work. What is left over needs a person asking again, once,
about this particular change.

So this is a *third* answer at the commit card, beside committing and not
committing: send the round instead. The work stays where it is, the navigator is
asked for evidence rather than reminded of rules, and whoever asked comes back
and commits afterwards — or does not.

Both files belong to the project, not to Halyard. What is worth asking again is
something a team learns about its own failures, and it changes as they learn.
Halyard carries them and does not read them:

- `inquiry` goes to the model while it writes the commit message, and asks for
  one more judgement — is this change worth a round? Its own instructions say
  how to answer and, importantly, when *not* to: a flag that fires on every
  commit is approved by reflex within a fortnight and the mechanism dies quietly.
- `review` is the round itself, and goes to the navigator when the button is
  pressed. Nothing here summarises it; it is sent whole.
"""

from __future__ import annotations

import logging
from pathlib import Path

from halyard.core.config_file import Confirmation

logger = logging.getLogger(__name__)

#: A round is a page, not a book. Past this something has gone wrong with the
#: file rather than with the change, and a session should not be handed it.
LIMIT = 40_000


def offered(confirmation: Confirmation | None) -> bool:
    """Whether this project has a round to offer at all."""
    return bool(confirmation and confirmation.review)


def _read(path: Path | None, project: Path | None) -> str:
    """One of the project's own files, or nothing.

    Relative to the project rather than to the Halyard checkout: these belong to
    the codebase being worked on, and `NOTES/development-standards/...` is how
    somebody will write it.

    Missing is not an error anybody should hear about mid-commit — it is a
    configuration mistake, said once in the log where it can be fixed.
    """
    if path is None or project is None:
        return ""
    wanted = path.expanduser()
    if not wanted.is_absolute():
        wanted = project / wanted
    try:
        text = wanted.read_text(encoding="utf-8").strip()
    except OSError as missing:
        logger.warning("Could not read the confirmation file %s: %s", wanted, missing)
        return ""
    if len(text) > LIMIT:
        logger.warning(
            "Confirmation file %s is %d characters; sending the first %d", wanted, len(text), LIMIT
        )
        return text[:LIMIT]
    return text


def inquiry(confirmation: Confirmation | None, project: Path | None) -> str:
    """What to ask the model on top of writing a message. Empty if nothing."""
    return _read(confirmation.inquiry if confirmation else None, project)


def review(confirmation: Confirmation | None, project: Path | None) -> str:
    """The round to hand the navigator. Empty if there is none to hand."""
    return _read(confirmation.review if confirmation else None, project)
