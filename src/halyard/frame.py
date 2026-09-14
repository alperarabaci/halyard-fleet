"""Where the work stands, as Halyard can see it for itself.

A check and a handoff both hand somebody something to judge, and both have to
say what it is about: which task, which branch, which revision of the tree, and
which revision of the project's own file that did the asking. The project's
handoff prompts refuse to guess — a field that is missing is reported as
unmeasured — so Halyard supplies what it can read without asking anybody: the
task from the branch name, the rule `/label` uses; the machine; HEAD; a
fingerprint of the files; what is changed on top of HEAD; and the commit that
last changed a file. They travel as an envelope around what is handed on.

**The files, not the commits.** Whether a check is looking at the code a report
was about is a question about files, and HEAD cannot answer it: the work sits
uncommitted for most of its life. Nor is HEAD a reference to keep — a branch is
squash-merged into main, and its commits are not there afterwards. So the
fingerprint is taken over the files themselves: staging, committing or squashing
them leaves it as it was, and any edit changes it.

Read from the project's working tree, never written to it. Git refreshes its
index on the way if it is let, and an agent committing at the same moment would
find it locked; so it is asked without optional locks, and through plumbing
where the porcelain ignores that — measured: `git diff` rewrote the index
regardless, `git diff-index` did not.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from halyard.tasks.branches import current, number_of

logger = logging.getLogger(__name__)

#: A project file handed to a model is a page of instructions. Past this the
#: file is wrong, not long.
LIMIT = 40_000

#: Reading a repository is local, and should never take long.
GIT_TIMEOUT = 10.0

#: How much of the fingerprint is shown: enough that two states of one branch
#: never share it by chance, short enough to compare by eye.
SHOWN = 12


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


def _run(path: Path, *args: str, stdin: str | None = None) -> str | None:
    """What git printed, or None if it could not answer."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            input=stdin,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _git(path: Path, *args: str) -> str:
    return (_run(path, *args) or "").strip()


def _paths(listed: str) -> list[str]:
    """Paths git listed with `-z`, one to a NUL."""
    return [entry for entry in listed.split("\0") if entry]


def host() -> str:
    """This machine's short name — which of a team's machines the tree is on.

    The same repository sits on more than one, and a report written on one of
    them is not about the files on another.
    """
    return socket.gethostname().split(".")[0]


@dataclass(frozen=True)
class Tree:
    """A working tree at one moment: the commit under it, and its files."""

    head: str
    #: A fingerprint of the files themselves, `SHOWN` characters of it.
    content: str
    #: Nothing changed on top of HEAD, and nothing untracked beside it.
    clean: bool = False


def tree(path: Path) -> Tree | None:
    """The commit under the working tree, and a fingerprint of its files.

    Every file git would commit, by path and by the id git gives its contents:
    what the index holds, with anything edited since read afresh, anything
    deleted left out, and anything untracked and not ignored added. So the same
    files give the same fingerprint however much of them is committed.

    None when git cannot say — no repository, no commit yet, or a question it
    did not answer in time — which the reader is told rather than left to take
    for a clean tree.
    """
    head = _git(path, "rev-parse", "--short", "HEAD")
    indexed = _run(path, "ls-files", "-s", "-z")
    edited = _run(path, "diff-files", "--name-only", "-z")
    deleted = _run(path, "ls-files", "-d", "-z")
    untracked = _run(path, "ls-files", "-o", "--exclude-standard", "-z")
    status = _run(path, "status", "--porcelain")
    if not head or None in (indexed, edited, deleted, untracked, status):
        return None

    blobs: dict[str, str] = {}
    for entry in _paths(indexed or ""):
        described, _, name = entry.partition("\t")
        blobs[name] = described.split()[1]
    gone = set(_paths(deleted or ""))
    for name in gone:
        blobs.pop(name, None)

    fresh: list[str] = []
    for name in dict.fromkeys([*_paths(edited or ""), *_paths(untracked or "")]):
        if name in gone:
            continue
        where = path / name
        if where.is_symlink():
            # Git keeps a link as the text it points at, and so does this.
            target = os.readlink(where).encode("utf-8", "surrogateescape")
            blobs[name] = hashlib.sha1(
                b"blob %d\0" % len(target) + target, usedforsecurity=False
            ).hexdigest()
        elif where.is_file():
            fresh.append(name)
        # A directory here is a submodule, which the index already names by commit.
    if fresh:
        hashed = _run(path, "hash-object", "--stdin-paths", stdin="".join(f"{n}\n" for n in fresh))
        ids = (hashed or "").split()
        if len(ids) != len(fresh):
            return None
        blobs.update(zip(fresh, ids, strict=True))

    listing = "".join(f"{blobs[name]} {name}\n" for name in sorted(blobs))
    content = hashlib.sha256(listing.encode("utf-8", "surrogateescape")).hexdigest()[:SHOWN]
    return Tree(head=head, content=content, clean=not (status or "").strip())


def envelope(facts: Sequence[str]) -> list[str]:
    """The facts around something handed on, as a Markdown list: one fact to a
    line, its name first, the way a model reads it best."""
    return ["Envelope:", *(f"- {fact}" for fact in facts)]


def context(path: Path, project: str, *, replied: str = "", reply: Tree | None = None) -> list[str]:
    """What Halyard can see for itself, one fact to a line.

    The work item comes from the branch name, the rule `/label` uses. The rest
    is where the repository stands right now — on which machine, at which
    revision, with which files, and what is changed on top of the revision —
    because a judgement of a report is only good for the tree that report was
    about. `replied` is when the reply being judged came in, and `reply` where
    the files stood then, recorded as it arrived: a check on a report three
    hours old, told nothing of it, set about rebuilding the tree it came from.
    """
    branch = current(path)
    number = number_of(branch or "")
    lines = [f"Project: {project}", f"Host: {host()}"]
    if number is not None:
        lines.append(f"Work item: {project}#{number}")
    lines.append(f"Branch: {branch or 'none (detached HEAD)'}")
    if head := _git(path, "rev-parse", "--short", "HEAD"):
        lines.append(f"HEAD: {head}")
    now = tree(path)
    if now is None:
        lines.append("Content: could not be read")
    else:
        lines.append(f"Content: {now.content}" + (" (clean)" if now.clean else ""))
    changed = _git(path, "diff-index", "-M", "--shortstat", "HEAD")
    lines.append(f"Uncommitted: {changed or 'nothing'}")
    if replied:
        lines.append(f"At the reply: {_since(replied, reply, now)}")
    return lines


def _since(replied: str, reply: Tree | None, now: Tree | None) -> str:
    """Where the files stood when the reply came in, and whether they still do."""
    if reply is None:
        return f"{replied} · not recorded"
    if now is None:
        verdict = "cannot compare"
    elif now.content == reply.content:
        verdict = "same files"
    else:
        verdict = "files changed since"
    return f"{replied} · HEAD {reply.head} · Content {reply.content} · {verdict}"


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
