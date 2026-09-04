"""What an agent is never allowed to do, whoever is asked.

The mirror of `writes.py` and `tools.py`. Those say *do not ask, allow*; this
says *do not ask, refuse* — and the gate has had no way to say that until now.
Everything else it decides is a question put to a person; this is a standing
answer given before anybody is asked.

One rule today, off by default: an agent may not commit or push. Halyard commits
on request from a phone, with the diff summarised and a message to approve, and
that is a different act from an agent deciding on its own that now is the moment
to write history. Somebody who wants their agents committing leaves the flag
alone and nothing changes.

**`/pause` does not lift it.** Pausing means "stop asking me", and it hands each
call back to the runtime's own permission list — which is the right answer for a
question, and the wrong one for a rule. A guard that a pause quietly switches
off is a guard nobody can rely on, so this is checked first of all.

**It is a guardrail, not a lock.** An agent that wanted to could write a script,
add an alias, or call a library. This stops the reflex — the agent that helpfully
commits because that is what one does after making a change — and it does not
pretend to stop anything determined.
"""

from __future__ import annotations

import re

#: `git commit`, `git push`, and the same with git's own options in front.
#:
#: Three things are deliberate, each measured against a command that should not
#: be refused:
#:
#: - It must start a command. Otherwise `echo git commit` — an agent writing
#:   documentation — is read as a commit.
#: - Options are matched explicitly rather than with a wildcard, so
#:   `git log --grep commit` stays a search: after `git` only a flag or
#:   `-C <path>` may precede the subcommand, and `log` is neither.
#: - The subcommand may not run on into a hyphen, so `git commit-tree` — a
#:   plumbing command that moves no branch — is left alone.
_WRITES_HISTORY = re.compile(
    r"(?:^|[\n;&|(])\s*(?:sudo\s+)?git\b(?:\s+(?:-[cC]\s+\S+|--\S+|-\w))*"
    r"\s+(?P<act>commit|push)(?![\w-])",
    re.IGNORECASE,
)


def writes_history(command: str) -> str | None:
    """`"commit"`, `"push"`, or None if this command does neither.

    Reads the whole command line, so `cd somewhere && git commit -m x` is caught
    along with the plain form. That is also why this cannot be a list of exact
    strings: what arrives is a shell line, not an argument list.
    """
    found = _WRITES_HISTORY.search(command or "")
    return found.group("act").lower() if found else None


def writes_history_if(command: str, refusing: bool) -> str | None:
    """Which act this is, when refusing is switched on. None otherwise.

    Two arguments rather than a check at the call site, so the flag and the
    pattern are read in one place and a caller cannot accidentally apply one
    without the other.
    """
    return writes_history(command) if refusing else None


def why(act: str) -> str:
    """What the agent is told, which is not the same as what is recorded.

    It says what to do instead. An agent told only "denied" tries the next
    spelling of the same command; one told who does commit here stops and says
    so to the person.
    """
    return (
        f"Refused: agents do not {act} in this project. "
        "Halyard commits on request from the phone — ask for it and it will be offered "
        "with the diff and a message to approve. Do not try another way of running this."
    )
