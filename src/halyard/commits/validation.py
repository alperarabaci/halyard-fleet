"""What has to be true before a commit is offered from a phone.

Two checks that answer to different standards, deliberately.

**The project's own commands** — `make test-fast`, or whatever it calls its
quick check — are facts. If one fails the code is broken, there is nothing to
weigh, and no card is offered at all. They are named by a project's
`commit_options:` and run as its other commands are, through
`halyard.commands`; absent by default, because a command invented on this
project's behalf would fail on every commit somewhere else. What this module
says about them is when they are not worth running: `only_documentation`.

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
from fnmatch import fnmatch

from halyard.commits.repository import Uncommitted

logger = logging.getLogger(__name__)

#: Changes that cannot break a test suite, so nothing is run for them.
#:
#: A `/commit` of one edited note used to start the project's whole gate and
#: leave somebody watching a phone for minutes, for a change no test in it could
#: have an opinion about. A commit option's inspections are skipped with its
#: commands: they read the change for what code can get wrong.
#:
#: Only those are skipped. The warnings still run, and the task-id one in
#: particular is *not* a false positive here: prompts and standards live in
#: markdown in the project this was built for, and those files carry the task id
#: like any other. It is searched in what was added, whatever the file is.
#:
#: Matched with `fnmatch`, whose `*` crosses directory separators, so `NOTES/*`
#: covers everything under it however deep.
DOCUMENTATION: tuple[str, ...] = (
    "*.md",
    "*.rst",
    "*.txt",
    "*.adoc",
    "LICENSE",
    "NOTES/*",
    "docs/*",
)

#: Where a diff says a line was added. Only additions are searched: the task id
#: disappearing from a line somebody deleted says nothing about the new work.
_ADDED = re.compile(r"^\+(?!\+\+)", re.MULTILINE)


@dataclass(frozen=True)
class Checked:
    """What the guesses found. `warnings` ride along on the card and stop
    nothing.

    Plain text. How it is marked up belongs to whichever channel is showing it.
    """

    warnings: tuple[str, ...] = ()


def only_documentation(work: Uncommitted, patterns: Sequence[str] = DOCUMENTATION) -> bool:
    """Whether everything this would commit is prose.

    All of it, not most: one `.py` beside four notes is a code change with
    documentation attached, and the check that would have caught it is exactly
    the one worth running.

    An empty change is not documentation — there is nothing to say about it —
    but that case is refused earlier anyway.
    """
    if not work.changes:
        return False
    return all(any(fnmatch(c.path, one) for one in patterns) for c in work.changes)


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


def check(work: Uncommitted, *, warn_if: Sequence[str] | None = None) -> Checked:
    """The warnings that apply to this work.

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

    return Checked(warnings=tuple(warnings))
