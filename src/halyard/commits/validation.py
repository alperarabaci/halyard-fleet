"""What has to be true before a commit is offered from a phone.

Two checks that answer to different standards, deliberately.

**The project's own command** — `make test-fast`, or whatever it calls its quick
check — is a fact. If it fails the code is broken, there is nothing to weigh,
and no card is offered at all. Configured per project and absent by default: a
command invented on this project's behalf would fail on every commit somewhere
else.

**The task id** is a guess, and is treated like one. An agent that has lost
sight of what it was asked tends to leave references to its own conversation in
the code — "as discussed above", "per the earlier note" — which read as
meaningful and are worth nothing to whoever finds them next year. The branch
already names the issue, so its number appearing in what was written is a cheap
sign the work stayed attached to the task. Its absence is a reason to look, not
a reason to stop: a commit that renames a file or fixes `.gitignore` will never
mention it and is perfectly good. So this warns on the card and the person
decides.

Facts block. Guesses warn. Which of the two a check is, is the only question
worth asking about it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from halyard.commits.repository import Uncommitted

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


def _tail(text: str, lines: int = FAILURE_LINES) -> str:
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def run(command: str, path: Path, *, timeout: float = VALIDATE_TIMEOUT) -> tuple[bool, str]:
    """Run the project's own check, and say whether it passed and what it said.

    Through a shell, because what is configured is a command line somebody
    types — `make test-fast`, `npm test && npm run lint` — and taking it apart
    into an argument list would refuse half of what people mean by it. It comes
    from this machine's own `halyard.yaml`, which is the same trust as the file
    that says where the code is.
    """
    try:
        done = subprocess.run(
            command,
            shell=True,
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"It did not finish within {timeout:.0f}s."
    except OSError as refused:
        return False, str(refused)
    if done.returncode == 0:
        return True, ""
    return False, _tail(f"{done.stdout}\n{done.stderr}")


def check(
    work: Uncommitted,
    path: Path,
    command: str | None,
    *,
    timeout: float = VALIDATE_TIMEOUT,
    run_check=run,
) -> Checked:
    """Everything that has to hold, in the order that wastes the least.

    The guess is evaluated first because it costs nothing, and the project's
    command last because it costs the most — but the cheap one cannot refuse,
    so the order changes no outcome, only how long a doomed commit takes to say
    so.
    """
    warnings: list[str] = []
    if not mentions_task(work, work.reference):
        number = (work.reference or "").rpartition("#")[2]
        warnings.append(f"{number} appears nowhere in the code this would commit")

    if not command:
        return Checked(warnings=tuple(warnings))

    passed, said = run_check(command, path, timeout=timeout)
    if not passed:
        return Checked(refused=command, output=said, warnings=tuple(warnings))
    return Checked(warnings=tuple(warnings), passed=command)
