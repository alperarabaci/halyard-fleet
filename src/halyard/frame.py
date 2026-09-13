"""Where the work stands, as Halyard can see it for itself.

A check and a handoff both hand somebody something to judge, and both have to
say what it is about: which task, which branch, which revision of the tree, and
which revision of the project's own file that did the asking. The project's
handoff prompts refuse to guess — a field that is missing is reported as
unmeasured — so Halyard supplies what it can read without asking anybody: the
task from the branch name, the rule `/label` uses; HEAD; what is changed on top
of it; and the commit that last changed a file.

Read from the project's working tree, never written to it.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from halyard.tasks.branches import current, number_of

logger = logging.getLogger(__name__)

#: A project file handed to a model is a page of instructions. Past this the
#: file is wrong, not long.
LIMIT = 40_000

#: Reading a repository is local, and should never take long.
GIT_TIMEOUT = 10.0


def read(path: Path, project: Path | None) -> str:
    """One of the project's own files, or nothing if it cannot be read.

    Relative to the project, which is where these files live. A missing one is
    said by the caller, and by `doctor` before that.
    """
    wanted = path.expanduser()
    if not wanted.is_absolute() and project is not None:
        wanted = project / wanted
    try:
        text = wanted.read_text(encoding="utf-8").strip()
    except OSError as missing:
        logger.warning("Could not read %s: %s", wanted, missing)
        return ""
    return text[:LIMIT]


def _git(path: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def context(path: Path, project: str) -> list[str]:
    """What Halyard can see for itself, one fact to a line.

    The work item comes from the branch name, the rule `/label` uses. The rest
    is where the repository stands right now — which revision, and what is
    changed on top of it — because a judgement of a report is only good for
    the tree that report was about.
    """
    branch = current(path)
    number = number_of(branch or "")
    lines = [f"Project: {project}"]
    if number is not None:
        lines.append(f"Work item: {project}#{number}")
    lines.append(f"Branch: {branch or 'none (detached HEAD)'}")
    if head := _git(path, "rev-parse", "--short", "HEAD"):
        lines.append(f"HEAD: {head}")
    lines.append(f"Uncommitted: {_git(path, 'diff', '--shortstat', 'HEAD') or 'nothing'}")
    return lines


def version(path: Path, project: Path) -> str:
    """Which revision of a project file is in use: the commit that last changed
    it, and whether it has been edited since.

    What a finding was a finding *by*, once the file changes next week.
    """
    commit = _git(project, "log", "-1", "--format=%h", "--", str(path))
    if not commit:
        return "uncommitted"
    edited = _git(project, "status", "--porcelain", "--", str(path))
    return f"{commit} + local edits" if edited else commit
