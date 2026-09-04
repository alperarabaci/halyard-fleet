"""Which task a branch is for.

A branch created from an issue is named for it — `320-rag-v4-pdf-report` — so
the number is there to be read and nobody has to type it.

**`commits.repository.reference_for` reads the same convention** and does not
call this, on purpose: that package needs only git and reaches no network, and
making it depend on the one that talks to an issue tracker would be the wrong
direction for a dependency. The cost is that the two must agree about what a
task-named branch looks like, which is why it is said here plainly and why both
say where the other is.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: A branch named for the issue it closes. GitLab writes these when a branch is
#: created from an issue, and GitHub does the same.
_NUMBERED = re.compile(r"^(\d+)[-_]")

GIT_TIMEOUT = 10.0


def number_of(branch: str) -> int | None:
    """The task this branch is for, or None if its name does not say."""
    found = _NUMBERED.match((branch or "").strip())
    return int(found.group(1)) if found else None


def current(path: Path) -> str | None:
    """The branch checked out here, or None on a detached head."""
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None
