"""Reading a git remote into the project it points at.

Everything `/label` needs about *where* comes from here, which is why none of
it is configured. The remote already says the host and the path — the two
questions a forge asks — and it says them for whichever project the chat is
about, without anybody keeping a second copy in `halyard.yaml` that can drift.

Both spellings, because a repository is cloned either way and the same person
switches between them:

    git@gitlab.com:agent-platform34/investment/alpha-engine.git
    https://gitlab.com/agent-platform34/investment/alpha-engine.git
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: `git@host:path.git`, the SSH spelling. Deliberately not a URL parser: this
#: form is not a URL, and the one place it looks like one is the colon that
#: separates the host from a path rather than a port.
_SSH = re.compile(r"^(?:ssh://)?(?:[^@/]+@)?(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$")

#: `scheme://host/path.git`, with any credentials in it ignored rather than
#: carried around.
_HTTP = re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Origin:
    """Where a repository came from."""

    host: str
    #: `group/subgroup/project`, as the forge names it.
    path: str


def read(remote: str) -> Origin | None:
    """The host and project a remote points at, or None if it is neither shape."""
    remote = (remote or "").strip()
    if not remote:
        return None
    for pattern in (_HTTP, _SSH):
        found = pattern.match(remote)
        if found and found.group("path"):
            return Origin(host=found.group("host").lower(), path=found.group("path"))
    return None


#: Reading a remote is a local operation and should never reach the network.
GIT_TIMEOUT = 10.0


def origin_of(path: Path, *, remote: str = "origin") -> Origin | None:
    """Where this checkout points, or None if it points nowhere recognisable.

    Asked of git rather than of configuration, so a repository whose remote
    somebody changed this morning is labelled against the project it is now
    part of rather than the one `halyard.yaml` remembers.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return read(done.stdout.strip()) if done.returncode == 0 else None
