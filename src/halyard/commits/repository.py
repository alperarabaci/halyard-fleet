"""Committing what an agent wrote, from a phone.

The last step of a day's work that never reached the phone. Everything else
does — approvals, questions, the sessions themselves — and then the branch sits
finished until somebody walks to the desk.

**What gets committed is the whole working tree**, tracked edits and new files
alike, minus whatever `.gitignore` already excludes. This was built the other
way first — commit only what was staged — on the assumption that staging is a
decision somebody makes at a keyboard and that carrying it out is the safe
thing. That assumption was wrong for the only workflow this exists for. A
navigator and a driver write code; neither of them stages anything; and a
control plane that answers "nothing is staged" to the person who asked it to
commit an afternoon of agent work has understood the job backwards.

So the rule is one sentence with no modes in it: everything git would see is
what gets committed. The card says what that is, and the new files are marked,
because a file that has never been committed before is the one worth a second
look on a small screen.

Reading a full diff on a phone is not a review, it is scrolling. A model
proposes the sentence; the phone shows the branch, the files and the wording; a
person says yes. The one thing never left to the model is the issue reference,
because a wrong number files work under somebody else's issue and reads as
correct forever after. That is computed from the branch name.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: How much of a diff is worth sending to a model. Generous — a commit message
#: is only as good as what the model was allowed to see — but bounded, because
#: a regenerated lockfile or a vendored directory would otherwise carry a
#: megabyte into a one-line request.
DIFF_LIMIT = 20_000

#: How many past subjects go along as the house style. Enough to show a
#: convention, few enough that one odd commit does not become the pattern.
STYLE_EXAMPLES = 20

#: Git is being asked to read local state, not to reach a network. Anything
#: slower than this is a repository problem worth surfacing rather than waiting
#: through on a phone.
GIT_TIMEOUT = 30.0

#: Pushing does reach a network, over a link nobody here controls. Generous
#: enough for a large first push on a slow connection, bounded so a phone is
#: told something rather than left holding.
PUSH_TIMEOUT = 180.0

#: How many lines of "what changed" to carry back to the phone. The subject
#: line says what the commit is called; this says what is in it, which is the
#: thing somebody away from the desk has no other way of knowing.
SUMMARY_LINES = 4

#: How much of a new file to show the model. A file nobody has committed before
#: has no diff of its own, so one is made — bounded here so a generated asset
#: cannot fill the request on its own.
NEW_FILE_LINES = 400

#: A branch named for the issue it closes. GitLab writes these when a branch is
#: created from an issue — `281-power-gen-minor-fixes` — and the number is the
#: only part worth keeping.
#:
#: `tasks.branches.number_of` reads the same convention and is deliberately not
#: called from here: this module needs only git and reaches no network, and
#: depending on the package that talks to an issue tracker would be the wrong
#: direction. If one of these changes, so must the other.
_ISSUE = re.compile(r"^(\d+)[-_]")

#: Files whose diffs say nothing a message could use, and cost the most to
#: send. Their names still appear on the card; only the body is held back.
_NOT_WORTH_READING = ("uv.lock", "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock")

#: Git's own mark for a file it has never seen.
UNTRACKED = "?"


#: Git, told never to ask a person anything.
#:
#: Nothing here has a terminal, and a `git push` with no stored credential wants
#: a username. It failed fast the first time this happened — "could not read
#: Username for 'https://gitlab.com': Device not configured" — but only because
#: there was no tty at all. Given one it would block, and given an askpass it
#: would put a dialog on a desktop nobody is sitting at. Either way a phone
#: waits out the timeout for an answer that was never coming.
#:
#: Set here rather than at the push, because a `git` that can prompt is wrong
#: everywhere in this module.
def _environment() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


Run = Callable[..., "subprocess.CompletedProcess[str]"]


class GitError(RuntimeError):
    """git refused, and its own words are the best explanation available."""


@dataclass(frozen=True)
class Change:
    """One file that would go into the commit."""

    status: str
    path: str
    insertions: int = 0
    deletions: int = 0

    @property
    def is_new(self) -> bool:
        return self.status == UNTRACKED


@dataclass(frozen=True)
class Uncommitted:
    """What a repository would commit right now, and whether it should.

    `blocked` is the whole safety story in one field: when it is set, nothing
    downstream may proceed and its text is what the phone is told. A detached
    head, a half-finished merge, a clean tree — all arrive the same way.
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

    @property
    def new_files(self) -> tuple[Change, ...]:
        """Files git has never seen. Worth naming separately on a small screen:
        an edit to a tracked file is the work, a brand new file might be the
        work or might be something that wandered in."""
        return tuple(c for c in self.changes if c.is_new)


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
        env=_environment(),
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


def _status(path: Path, *, run: Run | None = None) -> list[tuple[str, str]]:
    """Every path git would commit, as (status letter, path).

    Read with `-z` rather than by line. Filenames contain spaces, and the
    line-oriented form quotes and escapes them — which is another parser to get
    wrong, on the one input nobody controls.

    A rename writes two NUL-separated paths, new first. The new name is the one
    that exists now.
    """
    raw = _run(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", run=run)
    parts = raw.split("\0")
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        marks, name = entry[:2], entry[3:]
        if marks[0] == "R" or marks[1] == "R":
            # The old name follows in its own record, and is not a path to add.
            index += 1
        letter = UNTRACKED if marks == "??" else (marks[0] if marks[0] != " " else marks[1])
        found.append((letter, name))
    return found


def _counts(path: Path, *, run: Run | None = None) -> dict[str, tuple[int, int]]:
    """How far each tracked file moved, staged or not.

    `HEAD` rather than nothing: an agent that ran `git add` itself would
    otherwise have its work counted as zero.
    """
    counted: dict[str, tuple[int, int]] = {}
    for line in _run(path, "diff", "HEAD", "--numstat", run=run).splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        added, removed, name = fields[0], fields[1], fields[-1]
        # Binary files are counted as "-", which is not nothing changed.
        counted[name] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    return counted


def _new_file_lines(path: Path, name: str) -> list[str]:
    """A new file's contents, as far as they are worth reading.

    Returns nothing for anything that does not decode as text — a binary asset
    has no lines, and guessing at them would put noise in front of the model.
    """
    try:
        found = (path / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    return found.splitlines()[:NEW_FILE_LINES]


def _diff(path: Path, changes: Sequence[Change], *, run: Run | None = None) -> tuple[str, bool]:
    """The diff a model should read, and whether it was cut short.

    Two halves, because git only writes one of them. `diff HEAD` covers every
    tracked file; a file git has never seen appears in no diff at all, so one is
    written here — which matters more than it sounds, since an agent asked to
    build something new produces exactly that and nothing else.
    """
    worth = [c for c in changes if not c.path.endswith(_NOT_WORTH_READING)]
    tracked = [c.path for c in worth if not c.is_new]

    body = _run(path, "diff", "HEAD", "--", *tracked, run=run) if tracked else ""

    pieces = [body] if body else []
    for change in worth:
        if not change.is_new:
            continue
        lines = _new_file_lines(path, change.path)
        if not lines:
            continue
        pieces.append(
            f"--- /dev/null\n+++ b/{change.path}\n" + "\n".join(f"+{line}" for line in lines)
        )

    whole = "\n".join(pieces)
    if len(whole) <= DIFF_LIMIT:
        return whole, False
    return whole[:DIFF_LIMIT], True


def read(path: Path, project: str, *, run: Run | None = None) -> Uncommitted:
    """What this repository would commit, or why it must not.

    Every refusal is returned rather than raised: the phone is told the same
    way whichever thing is wrong, and a repository somebody moved reads as
    "not on this machine" instead of a traceback in a log.
    """
    try:
        if not path.exists():
            return Uncommitted(blocked=f"{path} is not on this machine any more.")
        _run(path, "rev-parse", "--git-dir", run=run)
    except (GitError, OSError):
        return Uncommitted(blocked=f"{path} is not a git repository.")

    try:
        if (busy := _in_progress(path, run=run)) is not None:
            return Uncommitted(
                blocked=f"This repository is in the middle of {busy}. Finish it at a desk."
            )

        try:
            branch = _run(path, "symbolic-ref", "--quiet", "--short", "HEAD", run=run).strip()
        except GitError:
            # Detached: a commit here belongs to no branch and is lost the next
            # time anything is checked out.
            return Uncommitted(
                blocked="HEAD is detached, so a commit here would belong to no branch."
            )

        counted = _counts(path, run=run)
        changes = tuple(
            Change(
                status=letter,
                path=name,
                insertions=counted.get(name, (len(_new_file_lines(path, name)), 0))[0],
                deletions=counted.get(name, (0, 0))[1],
            )
            for letter, name in _status(path, run=run)
        )
        if not changes:
            return Uncommitted(
                branch=branch,
                blocked="Nothing has changed on this branch. There is nothing to commit.",
            )

        diff, truncated = _diff(path, changes, run=run)
        style = tuple(
            line.strip()
            for line in _run(path, "log", f"-{STYLE_EXAMPLES}", "--format=%s", run=run).splitlines()
            if line.strip()
        )
    except (GitError, OSError) as refused:
        return Uncommitted(blocked=f"git could not read this repository: {refused}")

    return Uncommitted(
        branch=branch,
        reference=reference_for(project, branch),
        changes=changes,
        diff=diff,
        truncated=truncated,
        style=style,
    )


def prompt(work: Uncommitted) -> str:
    """What to ask a model for.

    It is asked for the description only, and told the reference will be put in
    front of it — a model that writes the number too produces
    `alpha-engine#281 alpha-engine#281 …`, which is the obvious failure and was
    worth one sentence to prevent.

    The house style is shown rather than described. alpha-engine writes
    `alpha-engine#281 short thing` and this repository writes prose; neither is
    hard-coded anywhere, and a project that changes its mind changes its own log.
    """
    parts = [
        "Describe a git commit of the changes below, for somebody who is away "
        "from their desk and has not seen any of this code.",
        "",
        "Answer in exactly this shape and nothing else:",
        "",
        "  <the subject line>",
        "  ---",
        f"  - <what changed, one short line>   (at most {SUMMARY_LINES} of these)",
        "",
        "The subject line comes first, on its own, with no quotes, no code "
        "fence and no trailing full stop. Then `---` on its own line. Then the "
        "lines saying what actually changed and why it matters — plain "
        "sentences about the work, not a list of filenames, which are already "
        "on screen.",
    ]
    if work.reference:
        parts += [
            "",
            f"Do NOT write the issue reference — `{work.reference}` is added in front of "
            "your line automatically. Write only what comes after it.",
        ]
    if work.style:
        shown = "\n".join(f"  {s}" for s in work.style)
        parts += [
            "",
            "Recent subjects from this repository. Match how they are written — "
            "their length, their mood, whether they name a scope:",
            shown,
        ]
    listed = "\n".join(f"  {c.status} {c.path}" for c in work.changes)
    parts += ["", f"Changed files on `{work.branch}` (? is a new file):", listed]
    if work.diff:
        parts += ["", "The changes:", "", work.diff]
        if work.truncated:
            parts += ["", "(The diff was cut short here; the file list above is complete.)"]
    return "\n".join(parts)


def summary_of(said: str) -> tuple[str, ...]:
    """The "what changed" lines, from what the model answered.

    Kept out of the commit itself. This repository writes one-line subjects and
    alpha-engine writes `alpha-engine#281 short thing`; a body neither of them
    has ever had would be this feature quietly changing how a project's history
    reads. It goes on the card, which is where the question was asked.
    """
    _, marker, rest = (said or "").partition("---")
    if not marker:
        return ()
    lines = []
    for line in rest.splitlines():
        cleaned = line.strip().lstrip("-*\u2022").strip()
        if cleaned:
            lines.append(cleaned)
    return tuple(lines[:SUMMARY_LINES])


def assemble(reference: str | None, said: str) -> str:
    """The model's line, made into the message that is actually committed.

    Tolerant of the model doing what it was asked not to: a line that already
    opens with the reference is not given a second one.
    """
    line = (said or "").partition("---")[0].strip().strip("`").strip()
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
    """Add everything git can see, commit it, and return the short sha.

    `add -A` rather than a list of the paths the card showed. The two agree in
    every ordinary case, and where they do not — a file written in the seconds
    between the card and the tap — committing the branch as it actually is
    beats committing a photograph of how it used to be. `.gitignore` is what
    decides the boundary, as it does for anybody typing this at a desk.
    """
    _run(path, "add", "-A", run=run)
    _run(path, "commit", "-m", message, run=run)
    return _run(path, "rev-parse", "--short", "HEAD", run=run).strip()


def push(path: Path, branch: str, *, run: Run | None = None) -> str:
    """Send the branch to `origin`, and say where it landed.

    `--set-upstream` every time. A branch created from a GitLab issue and
    checked out locally usually has an upstream already, and one an agent made
    on the machine does not — the flag is harmless where it is redundant and is
    the difference between working and "no upstream configured" where it is not.

    Never `--force`, and nothing here takes an argument that could become one.
    A rejected push is a real answer: somebody else moved the branch, and that
    is not a thing to resolve from a phone.
    """
    runner = run or subprocess.run
    done = runner(
        ["git", "-C", str(path), "push", "--set-upstream", "origin", branch],
        capture_output=True,
        text=True,
        timeout=PUSH_TIMEOUT,
        check=False,
        env=_environment(),
    )
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        # git writes the useful part of a rejection last, after the advice.
        raise GitError(said[-1] if said else "git push failed")
    return f"origin/{branch}"
