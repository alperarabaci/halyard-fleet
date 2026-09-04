"""What has to be true before a commit is offered from a phone.

Two checks that answer to different standards, deliberately.

**The project's own command** — `make test-fast`, or whatever it calls its quick
check — is a fact. If it fails the code is broken, there is nothing to weigh,
and no card is offered at all. Configured per project and absent by default: a
command invented on this project's behalf would fail on every commit somewhere
else.

**The warnings** are guesses, and are treated like them. They are also
*somebody's convention* rather than a truth about software, so they are named
and chosen rather than built in: `warn_if:` on a project picks which run, and
adding one is a function and a name in `WARNINGS` below.

The one shipped today is `task-id-missing`, and it is worth saying whose it is.
This project's branches are named for the issue they close, and an agent that
has lost sight of what it was asked tends to leave references to its own
conversation in the code — "as discussed above" — which read as meaningful and
are worth nothing to whoever finds them next year. The branch already names the
issue, so its number turning up in what was written is a cheap sign the work
stayed attached to the task.

Measured before it was made a warning rather than a refusal: of 28 real commits
in the repository this was written for, 24 mention their task id in the diff and
4 do not. Blocking would have refused one legitimate commit in seven.

Facts block. Guesses warn. Which of the two a check is, is the only question
worth asking about it — and a guess that is really a house style should be one
somebody can turn off.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from halyard.commands.running import run as commands_run
from halyard.commits.repository import Uncommitted

logger = logging.getLogger(__name__)

#: How long a project's check may take before the phone is told it did not
#: finish. Generous, because "fast" means different things to different test
#: suites, and bounded because somebody is holding a phone waiting for a card.
VALIDATE_TIMEOUT = 600.0

#: How much of a failing run to send. The last lines, because that is where a
#: test runner puts what failed; the rest is a phone-shaped wall of dots.
FAILURE_LINES = 25

#: Where a diff says a line was added. Only additions are searched: the task id
#: disappearing from a line somebody deleted says nothing about the new work.
_ADDED = re.compile(r"^\+(?!\+\+)", re.MULTILINE)


@dataclass(frozen=True)
class Checked:
    """What the checks found.

    `refused` is set only by a fact — it means no card, and it says which check
    said no. `warnings` ride along on the card and stop nothing.

    All plain text. How any of it is marked up belongs to whichever channel is
    showing it, and a test runner's output is full of angle brackets that would
    otherwise arrive as broken HTML.
    """

    #: The command that refused, or None if nothing did.
    refused: str | None = None
    #: The tail of what it printed, for somebody who has to guess why.
    output: str = ""
    warnings: tuple[str, ...] = ()
    #: What ran and passed, so the card can say so. Silence about a check that
    #: passed would leave somebody wondering whether it ran at all.
    passed: str | None = None


def mentions_task(work: Uncommitted, reference: str | None) -> bool:
    """Whether the work itself mentions the task it belongs to.

    Searched in the added lines, not in the commit message: the message always
    carries the reference because this codebase puts it there, so checking it
    would be checking our own handiwork and passing every time.

    Both spellings count — the bare number and the full `project#320` — because
    a comment reads better as one and a docstring as the other, and this is
    looking for attachment to the task rather than for a format.
    """
    if not reference:
        return True
    number = reference.rpartition("#")[2]
    if not number:
        return True
    added = "\n".join(line for line in work.diff.splitlines() if _ADDED.match(line))
    if not added:
        return True
    return bool(
        re.search(rf"(?<!\d){re.escape(number)}(?!\d)", added) or reference.lower() in added.lower()
    )


def _task_id_missing(work: Uncommitted) -> str | None:
    """`task-id-missing` — the work never names the issue its branch is for."""
    if mentions_task(work, work.reference):
        return None
    number = (work.reference or "").rpartition("#")[2]
    return f"{number} appears nowhere in the code this would commit"


#: Every warning that can be asked for, by the name `warn_if:` uses.
#:
#: A registry rather than a sequence of `if`s in `check`, so that a second
#: opinion about what is worth flagging is a new entry here and nothing else —
#: and so that the one below can be switched off by whoever does not share it.
WARNINGS: dict[str, Callable[[Uncommitted], str | None]] = {
    "task-id-missing": _task_id_missing,
}

#: What runs when a project says nothing. The house style of the repository this
#: was built for, which is a defensible default and a poor law.
DEFAULT_WARNINGS: tuple[str, ...] = ("task-id-missing",)


def _tail(text: str, lines: int = FAILURE_LINES) -> str:
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def run(command: str, path: Path, *, timeout: float = VALIDATE_TIMEOUT) -> tuple[bool, str]:
    """Run the project's check, through the one runner both callers share.

    Kept as a function here rather than calling `commands.run` from `check`
    directly, so a test can substitute it without reaching into another package.
    """
    result = commands_run(command, path, timeout=timeout)
    return result.ok, result.output


def check(
    work: Uncommitted,
    path: Path,
    command: str | None,
    *,
    warn_if: Sequence[str] | None = None,
    timeout: float = VALIDATE_TIMEOUT,
    run_check=run,
) -> Checked:
    """Everything that has to hold, in the order that wastes the least.

    The guesses are evaluated first because they cost nothing, and the project's
    command last because it costs the most — but the cheap ones cannot refuse,
    so the order changes no outcome, only how long a doomed commit takes to say
    so.

    `warn_if` names which warnings apply; `None` means the default set. A name
    nobody recognises is logged and skipped rather than raised — a typo in a
    list of opinions is not worth losing the ability to commit over.
    """
    warnings: list[str] = []
    for name in DEFAULT_WARNINGS if warn_if is None else warn_if:
        found = WARNINGS.get(name)
        if found is None:
            logger.warning("Ignoring unknown warning %r; known: %s", name, ", ".join(WARNINGS))
            continue
        if (said := found(work)) is not None:
            warnings.append(said)

    if not command:
        return Checked(warnings=tuple(warnings))

    passed, said = run_check(command, path, timeout=timeout)
    if not passed:
        return Checked(refused=command, output=said, warnings=tuple(warnings))
    return Checked(warnings=tuple(warnings), passed=command)
