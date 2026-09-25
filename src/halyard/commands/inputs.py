"""A command line that takes a value somebody types.

    commands:
      cleanup: make cleanup
      pull-branch: make pull-branch TASK={input.task}
      next-task: [cleanup, pull-branch]

Named in full, `input.` and all, the way `{label_groups.…}` is: the prefix says
where the value comes from — here, from whoever runs the command. `/command
next-task` asks for `task` before anything runs, so a list does not stop half
way to wait for an answer; `/command next-task 369` gives it outright.

Only `/command` runs these. A transition or a commit's `validate:` has nobody to
ask, and naming such a command there is refused when the file is read.

What was typed goes into the line quoted for the shell, so it arrives as one
word whatever it holds. Checking that it is a sensible value — a task number,
say — is the command's own business.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

#: `{input.task}` — the name is what the question asks for.
PLACEHOLDER = re.compile(r"\{input\.([^{}\s]+)\}")


def names_in(line: str) -> tuple[str, ...]:
    """The values a command line asks for, in order, each once."""
    return tuple(dict.fromkeys(PLACEHOLDER.findall(line or "")))


def filled(line: str, values: Mapping[str, str]) -> str:
    """The line with each typed value in place, quoted for the shell. A name
    with no value is left as written."""
    return PLACEHOLDER.sub(
        lambda found: (
            shlex.quote(values[found.group(1)]) if found.group(1) in values else found.group(0)
        ),
        line,
    )
