"""Where the work stands, as Halyard can see it for itself.

A check and a handoff both hand somebody something to judge, and both have to
say what it is about: which task, which branch, which revision of the tree, and
which revision of the project's own file that did the asking. The project's
handoff prompts refuse to guess — a field that is missing is reported as
unmeasured — so Halyard supplies what it can read without asking anybody: the
task from the branch name, the rule `/label` uses; the machine; HEAD; the tree
id of the files; what is changed on top of HEAD; and the commit that last
changed a file. They travel as an envelope around what is handed on.

**The files, not the commits.** Whether a check is looking at the code a report
was about is a question about files, and HEAD cannot answer it: the work sits
uncommitted for most of its life. Nor is HEAD a reference to keep — a branch is
squash-merged into main, and its commits are not there afterwards. So `Content`
is the tree git would write for the files as they stand — `git write-tree`,
which anybody can run and compare, where a fingerprint of Halyard's own could be
checked by nobody else. Staging or committing the files leaves it as it was, a
clean tree's is `HEAD^{tree}`, and any edit changes it.

Nothing in the working tree or the index is written. Git refreshes its index
on the way if it is let, and a commit made from the phone at that moment would
find it locked; so it is asked without optional locks, and through plumbing
where the porcelain ignores that — measured: `git diff` rewrote the index
regardless, `git diff-index` did not — and the tree is written from a copy of
the index. What that leaves is what `git add` would: objects in the repository's
store, referenced by nothing, which git's own collection clears.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from halyard.tasks.branches import current, number_of

logger = logging.getLogger(__name__)

#: A project file handed to a model is a page of instructions. Past this the
#: file is wrong, not long.
LIMIT = 40_000

#: Reading a repository is local, and should never take long.
GIT_TIMEOUT = 10.0

#: How much of the tree id is shown: enough that two states of one branch never
#: share it by chance, short enough to compare by eye.
SHOWN = 12

#: How `Content` is computed, said on the envelope so a reader can check it.
METHOD = "git write-tree"


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


def _run(
    path: Path, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
) -> str | None:
    """What git printed, or None if it could not answer."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            input=stdin,
            env={**os.environ, **env} if env else None,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return (_run(path, *args, env=env) or "").strip()


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
    #: The files' own tree id — what `git write-tree` gives for them — `SHOWN`
    #: characters of it.
    content: str
    #: The tree is HEAD's own: nothing changed on top of it, nothing untracked.
    clean: bool = False


def tree(path: Path) -> Tree | None:
    """The commit under the working tree, and the tree id of its files.

    What `git write-tree` gives for the working tree as it stands: a copy of the
    index, everything added to it — edits, deletions, and whatever is untracked
    and not ignored — and the tree written. The same command a team can put in
    its own documents and run by hand:

        T=$(mktemp); cp .git/index "$T"; GIT_INDEX_FILE="$T" git add -A;
        GIT_INDEX_FILE="$T" git write-tree; rm -f "$T"

    A copy of the index rather than an empty one, so git hashes only what has
    changed; the tree is the same either way.

    None when git cannot say — no repository, no commit yet, or a question it
    did not answer in time — which the reader is told rather than left to take
    for a clean tree.
    """
    head = _git(path, "rev-parse", "--short", "HEAD")
    under = _git(path, "rev-parse", "HEAD^{tree}")
    index = _git(path, "rev-parse", "--git-path", "index")
    if not head or not under or not index:
        return None
    with tempfile.TemporaryDirectory(prefix="halyard-tree-") as scratch:
        copy = Path(scratch) / "index"
        real = Path(index) if Path(index).is_absolute() else path / index
        try:
            shutil.copyfile(real, copy)
        except FileNotFoundError:
            pass  # No index yet: an empty one is where git itself would start.
        except OSError:
            return None
        separate = {"GIT_INDEX_FILE": str(copy)}
        if _run(path, "add", "-A", env=separate) is None:
            return None
        written = _git(path, "write-tree", env=separate)
    if not written:
        return None
    return Tree(head=head, content=written[:SHOWN], clean=written == under)


def envelope(facts: Sequence[str]) -> list[str]:
    """The facts around something handed on, as a Markdown list: one fact to a
    line, its name first, the way a model reads it best."""
    return ["Envelope:", *(f"- {fact}" for fact in facts)]


def context(
    path: Path,
    project: str,
    *,
    replied: str = "",
    reply: Tree | None = None,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """What Halyard can see for itself, one fact to a line.

    The work item comes from the branch name, the rule `/label` uses. The rest
    is where the repository stands right now — on which machine, at which
    revision, with which files, and what is changed on top of the revision —
    because a judgement of a report is only good for the tree that report was
    about. `replied` is when the reply being judged came in, and `reply` where
    the files stood then, recorded as it arrived: a check on a report three
    hours old, told nothing of it, set about rebuilding the tree it came from.
    `labels` are the task's own, one from each of the project's
    `label_groups:`, read from its tracker by whoever is asking.
    """
    branch = current(path)
    number = number_of(branch or "")
    lines = [f"Project: {project}", f"Host: {host()}"]
    if number is not None:
        lines.append(f"Work item: {project}#{number}")
    lines += [f"{group}: {label}" for group, label in (labels or {}).items()]
    lines.append(f"Branch: {branch or 'none (detached HEAD)'}")
    if head := _git(path, "rev-parse", "--short", "HEAD"):
        lines.append(f"HEAD: {head}")
    now = tree(path)
    if now is None:
        lines.append("Content: could not be read")
    else:
        lines.append(f"Content: {now.content} ({METHOD}{', clean' if now.clean else ''})")
    lines.append(f"Uncommitted: {_uncommitted(path)}")
    if replied:
        lines.append(f"At the reply: {_since(replied, reply, now)}")
    return lines


def _uncommitted(path: Path) -> str:
    """What is on top of HEAD: git's summary of the tracked files, and how many
    files are new to it.

    The new ones are counted apart because that summary leaves them out, and
    `nothing` said beside three new files reads as no change at all — while
    `Content` holds them, as `git add -A` does. What is ignored is left out of
    both.
    """
    changed = _git(path, "diff-index", "-M", "--shortstat", "HEAD")
    listed = _run(path, "ls-files", "--others", "--exclude-standard", "-z") or ""
    count = sum(1 for name in listed.split("\0") if name)
    new = f"{count} untracked file{'' if count == 1 else 's'}" if count else ""
    return " · ".join(part for part in (changed, new) if part) or "nothing"


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
