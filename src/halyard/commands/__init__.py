"""Running a project's own commands from a phone.

Separate from `halyard.commits`, which happens to use the same primitive. A
commit is one act with a fixed shape; this is whatever a project put in its
Makefile, named in `halyard.yaml` and picked off a list — `make test-all`,
`make bootstrap-up`, `make cleanup-merged-branches`.

Two pieces:

- `running` — how a command is run and what comes back. Used by
  `commits.validation` too, because copying twenty-five lines of subprocess
  handling is worse than depending on them.
- `catalogue` — which commands a project offers, from its `commands:` block.
"""

from halyard.commands.catalogue import Command, offered, resolve
from halyard.commands.running import (
    DEFAULT_TIMEOUT,
    LINES_WHEN_IT_FAILED,
    LINES_WHEN_IT_PASSED,
    Result,
    run,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "LINES_WHEN_IT_FAILED",
    "LINES_WHEN_IT_PASSED",
    "Command",
    "Result",
    "offered",
    "resolve",
    "run",
]
