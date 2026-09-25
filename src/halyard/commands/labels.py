"""A command line that takes a value from the task's labels.

Some commands need to know what the work is about before they can run it —
which part of a codebase an end-to-end suite should cover. The branch name was
the first idea and does not hold: not every branch can say it, and what one says
is a spelling, not a fact anybody chose. The task can: somebody labels it when
the work is planned, from a list the project wrote down.

    label_groups:
      area: [area:api, area:web, area:all]
    commands:
      e2e: make test-e2e SCOPE={label_groups.area}

The command names the group, and the group is the list. What goes in the line
is the part of each label after its last colon — `area:api` gives `api` — and
every label the task carries from the group, in the group's own order, joined
with commas: `api,web`. The group is named in full, `label_groups.` and all, so
somebody reading the line can see where the value comes from, and a shell brace
of its own — `${HOME}`, `find -exec {}` — is never mistaken for one.

Nothing here reads a tracker or asks anybody. Whoever runs the command brings
the labels, and says what to do about a group the task carries nothing from.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence

#: `{label_groups.area}` — a group's name is whatever `label_groups:` calls it,
#: up to the brace.
PLACEHOLDER = re.compile(r"\{label_groups\.([^{}\s]+)\}")


def groups_in(line: str) -> tuple[str, ...]:
    """The label groups a command line takes a value from, in order, each once."""
    return tuple(dict.fromkeys(PLACEHOLDER.findall(line or "")))


def value_of(label: str) -> str:
    """What one label gives a command: `area:api` → `api`, `level::3` → `3`,
    and a label with no colon gives itself."""
    return label.rpartition(":")[2].strip()


def values_from(groups: Mapping[str, Sequence[str]], carried: Sequence[str]) -> dict[str, str]:
    """Each group's value from the labels a task carries.

    Every label of the group the task has, in the group's own order and spelled
    as the group spells it, joined with commas. A group the task carries nothing
    from is left out, so a missing value is told apart from an empty one.
    """
    on_task = {label.casefold() for label in carried}
    found: dict[str, str] = {}
    for group, labels in groups.items():
        values = [value_of(label) for label in labels if label.casefold() in on_task]
        if values := [value for value in values if value]:
            found[group] = ",".join(dict.fromkeys(values))
    return found


def missing(line: str, values: Mapping[str, str]) -> tuple[str, ...]:
    """The groups a command line needs that have no value yet."""
    return tuple(group for group in groups_in(line) if group not in values)


def filled(line: str, values: Mapping[str, str]) -> str:
    """The line with each group's value in place, quoted for the shell.

    Quoted although a label is something the project wrote down: the line runs
    through a shell, and a label with a space in it should arrive as one word.
    A group with no value is left as written — `missing` says which.
    """
    return PLACEHOLDER.sub(
        lambda found: (
            shlex.quote(values[found.group(1)]) if found.group(1) in values else found.group(0)
        ),
        line,
    )
