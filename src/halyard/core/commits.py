"""Committing what is already staged, from a phone.

Everything else in a day's work reaches the phone — approvals, questions, the
sessions themselves — and then stops at the one step nobody can do from away.
The work is finished, staged, and sits there until somebody walks to the desk.

What makes that safe to close is that the decision has already been made. The
staging area is a choice made at the keyboard, deliberately, with the whole
diff in view; this only carries out what that choice already said. So nothing
here stages, unstages, or reaches for a file that was left out — if nothing is
staged there is nothing to commit, and that is an answer rather than a problem
to solve.

Reading a full diff on a phone is not worth anybody's time, which is the other
half of the design: a model proposes the sentence, the phone shows what is
being committed and what it would be called, and a person says yes. The one
thing never left to the model is the issue reference, because a wrong number
files work under somebody else's issue and reads as correct forever after.
That is computed from the branch name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: How much of a diff is worth sending to a model. Generous — a commit message
#: is only as good as what the model was allowed to see — but bounded, because
#: a staged lockfile or a vendored directory would otherwise carry a megabyte
#: into a one-line request.
DIFF_LIMIT = 20_000

#: How many past subjects go along as the house style. Enough to show a
#: convention, few enough that one odd commit does not become the pattern.
STYLE_EXAMPLES = 20

#: Git is being asked to read local state, not to reach a network. Anything
#: slower than this is a repository problem worth surfacing rather than waiting
#: through on a phone.
GIT_TIMEOUT = 20.0

#: A branch named for the issue it closes. GitLab writes these when a branch is
#: created from an issue — `281-power-gen-minor-fixes` — and the number is the
#: only part worth keeping.
_ISSUE = re.compile(r"^(\d+)[-_]")

#: Files whose diffs say nothing a message could use, and cost the most to
#: send. Their names still appear on the card; only the body is held back.
_NOT_WORTH_READING = ("uv.lock", "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock")

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class GitError(RuntimeError):
    """git refused, and its own words are the best explanation available."""


@dataclass(frozen=True)
class Change:
    """One staged file."""

    status: str
    path: str
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class Staged:
    """What a repository would commit right now, and whether it should.

    `blocked` is the whole safety story in one field: when it is set, nothing
    downstream may proceed, and its text is what the phone is told. Everything
    that could make a commit the wrong thing to do — a detached head, a
    half-finished merge, an empty staging area — arrives the same way.
    """

    branch: str = ""
    reference: str | None = None
    changes: tuple[Change, ...] = ()
    diff: str = ""
    #: True when the diff was cut at `DIFF_LIMIT`, so the model can be told it
    #: is looking at part of the picture rather than guessing from a fragment.
    truncated: bool = False
    style: tuple[str, ...] = field(default=())
    blocked: str | None = None

    @property
    def insertions(self) -> int:
        return sum(c.insertions for c in self.changes)

    @property
    def deletions(self) -> int:
        return sum(c.deletions for c in self.changes)


def _run(path: Path, *args: str, run: Run | None = None) -> str:
    """One git command, by argument list.

    Never a shell string. The commit message is written by a model and then
    typed over by a person, and both of those reach `git commit -m` — through
    argv, where a backtick is a backtick.
    """
    runner = run or subprocess.run
    done = runner(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        raise GitError(said[0] if said else f"git {args[0]} failed")
    return done.stdout


def reference_for(project: str, branch: str) -> str | None:
    """`alpha-engine#281`, from the project and a branch named for an issue.

    Computed rather than asked for. A model given the branch name will usually
    get this right, and "usually" is not good enough for a number that files
    the work under an issue and is never read again.

    A branch that is not named for an issue — `feat/runtime-isolation` — has no
    reference, and the message is left to stand on its own.
    """
    found = _ISSUE.match(branch.strip())
    return f"{project}#{found.group(1)}" if found and project else None


def _in_progress(path: Path, *, run: Run | None = None) -> str | None:
    """What git is in the middle of, if anything.

    Committing during a merge or a rebase means finishing somebody else's
    commit with a message written for a different one — recoverable, but not
    from a phone.
    """
    try:
        git_dir = Path(_run(path, "rev-parse", "--absolute-git-dir", run=run).strip())
    except GitError:
        return None
    for marker, what in (
        ("MERGE_HEAD", "a merge"),
        ("CHERRY_PICK_HEAD", "a cherry-pick"),
        ("REVERT_HEAD", "a revert"),
        ("rebase-merge", "a rebase"),
        ("rebase-apply", "a rebase"),
    ):
        if (git_dir / marker).exists():
            return what
    return None


def _changes(path: Path, *, run: Run | None = None) -> tuple[Change, ...]:
    """The staged files, with how much each moved."""
    counted: dict[str, tuple[int, int]] = {}
    for line in _run(path, "diff", "--staged", "--numstat", run=run).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, name = parts[0], parts[1], parts[-1]
        # Binary files are counted as "-", which is not nothing changed.
        counted[name] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )

    changes = []
    for line in _run(path, "diff", "--staged", "--name-status", run=run).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # A rename arrives as `R100\told\tnew`; the new name is what it is now.
        status, name = parts[0][:1], parts[-1]
        added, removed = counted.get(name, (0, 0))
        changes.append(Change(status=status, path=name, insertions=added, deletions=removed))
    return tuple(changes)


def _diff(path: Path, changes: Sequence[Change], *, run: Run | None = None) -> tuple[str, bool]:
    """The staged diff a model should read, and whether it was cut short."""
    worth = [c.path for c in changes if not c.path.endswith(_NOT_WORTH_READING)]
    if not worth:
        return "", False
    body = _run(path, "diff", "--staged", "--", *worth, run=run)
    if len(body) <= DIFF_LIMIT:
        return body, False
    return body[:DIFF_LIMIT], True


def read(path: Path, project: str, *, run: Run | None = None) -> Staged:
    """What this repository would commit, or why it must not.

    Every refusal is returned rather than raised: the phone is told the same
    way whichever thing is wrong, and a repository somebody moved or deleted
    reads as "not a git repository" instead of a traceback in the log.
    """
    try:
        if not path.exists():
            return Staged(blocked=f"{path} is not on this machine any more.")
        _run(path, "rev-parse", "--git-dir", run=run)
    except (GitError, OSError):
        return Staged(blocked=f"{path} is not a git repository.")

    try:
        if (busy := _in_progress(path, run=run)) is not None:
            return Staged(
                blocked=f"This repository is in the middle of {busy}. Finish it at a desk."
            )

        try:
            branch = _run(path, "symbolic-ref", "--quiet", "--short", "HEAD", run=run).strip()
        except GitError:
            # Detached: a commit here belongs to no branch and is lost the next
            # time anything is checked out.
            return Staged(blocked="HEAD is detached, so a commit here would belong to no branch.")

        changes = _changes(path, run=run)
        if not changes:
            return Staged(
                branch=branch,
                blocked="Nothing is staged. Halyard commits what you staged at the desk — "
                "it will not choose the files for you.",
            )

        diff, truncated = _diff(path, changes, run=run)
        style = tuple(
            line.strip()
            for line in _run(path, "log", f"-{STYLE_EXAMPLES}", "--format=%s", run=run).splitlines()
            if line.strip()
        )
    except (GitError, OSError) as refused:
        return Staged(blocked=f"git could not read this repository: {refused}")

    return Staged(
        branch=branch,
        reference=reference_for(project, branch),
        changes=changes,
        diff=diff,
        truncated=truncated,
        style=style,
    )


def prompt(staged: Staged) -> str:
    """What to ask a model for.

    It is asked for the description only, and told the reference will be put in
    front of it — a model that writes the number too produces
    `alpha-engine#281 alpha-engine#281 …`, which is the obvious failure and was
    worth one sentence to prevent.

    The house style is shown rather than described. This repository writes
    prose subjects and alpha-engine writes `alpha-engine#281 short thing`;
    neither is hard-coded anywhere, and a project that changes its mind changes
    its own log.
    """
    parts = [
        "Write the subject line for a git commit of the staged changes below.",
        "",
        "Answer with that one line and nothing else. No quotes, no code fence, "
        "no explanation, no trailing full stop.",
    ]
    if staged.reference:
        parts += [
            "",
            f"Do NOT write the issue reference — `{staged.reference}` is added in front of "
            "your line automatically. Write only what comes after it.",
        ]
    if staged.style:
        shown = "\n".join(f"  {s}" for s in staged.style)
        parts += [
            "",
            "Recent subjects from this repository. Match how they are written — "
            "their length, their mood, whether they name a scope:",
            shown,
        ]
    listed = "\n".join(f"  {c.status} {c.path}" for c in staged.changes)
    parts += ["", f"Staged files on `{staged.branch}`:", listed]
    if staged.diff:
        parts += ["", "The staged diff:", "", staged.diff]
        if staged.truncated:
            parts += ["", "(The diff was cut short here; the file list above is complete.)"]
    return "\n".join(parts)


def assemble(reference: str | None, said: str) -> str:
    """The model's line, made into the message that is actually committed.

    Tolerant of the model doing what it was asked not to: a line that already
    opens with the reference is not given a second one.
    """
    line = (said or "").strip().strip("`").strip()
    # Models reach for a code fence even when told not to; take the first line
    # that is not one rather than committing "```".
    for candidate in line.splitlines():
        candidate = candidate.strip().strip("`").strip()
        if candidate:
            line = candidate
            break
    if not reference:
        return line
    if line.lower().startswith(reference.lower()):
        return line
    return f"{reference} {line}" if line else reference


def commit(path: Path, message: str, *, run: Run | None = None) -> str:
    """Make the commit, and return the short sha.

    `--only` with no paths would mean "commit nothing"; the plain form commits
    the index, which is exactly what was shown and approved. Nothing is added
    here, so anything staged after the card was rendered would ride along —
    which is why the sha comes back, and why the card names the branch.
    """
    _run(path, "commit", "-m", message, run=run)
    return _run(path, "rev-parse", "--short", "HEAD", run=run).strip()
