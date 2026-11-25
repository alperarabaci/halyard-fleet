"""What part of a branch each inspection is to read: what changed since it last read.

An inspection used to be told only what was uncommitted. Seen on a branch of
eleven commits: the delivery was committed before its close, and the close's
inspections had three lines to read — the delivery, 28 files, was in no
inspection's envelope. Told the whole branch instead, an inspection at a
branch's fifth close reads the four before it again, and a close runs seven.

So each inspection is told two ranges, as commands it can run as they are: the
branch since its base, which every transition's envelope carries, and its own
share — what changed since it last ran here, for this transition. Its mark moves
only when every read it asked for ran: one whose model did not answer, or whose
reads did not all run, leaves its mark where it was, and the next run's share
is wider.

It is handed its own last answer too, with the range that answer was on. An
open finding, or a range it could not measure, is carried by the inspection
itself: what it says about either is the project's to say, and Halyard reads
none of it.

The mark is the tree the inspection was given, not a commit: work inspected
before it was committed is not inspected again for being committed after.

Kept per machine, beside Halyard's database. A machine that has not run an
inspection on a branch has no mark for it, and its share is the base's range.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from halyard import frame

logger = logging.getLogger(__name__)

#: Most characters kept of an inspection's last answer. It is handed back to
#: that inspection alone, and a whole answer is a page or two.
ANSWER_LIMIT = 8_000

#: How many marks are kept: one per inspection, per transition, per branch.
MARKS = 500

#: The envelope line each inspection's share is said on.
SINCE = "Since this inspection last ran here"


@dataclass(frozen=True)
class Mark:
    """Where an inspection last read a branch whole, and what it said."""

    at: datetime
    #: The commit and the tree it was given, in full.
    head: str
    tree: str
    #: The commit the branch then shared with its base, or empty.
    base: str
    #: The range it was given, as the command that shows it.
    given: str
    answer: str


@dataclass(frozen=True)
class Share:
    """What one inspection is told of its share."""

    #: Its line in the envelope.
    line: str
    #: The range, as the command that shows it, or empty when there is none.
    given: str
    #: Its last answer here, introduced, or empty when it has none.
    earlier: str


def _key(branch: str, transition: str, inspection: str) -> str:
    return f"{branch}|{transition}|{inspection}"


def _load(where: Path) -> dict:
    try:
        if not where.is_file():
            return {}
        loaded = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        logger.warning("Could not read %s: %s", where, unreadable)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def last(where: Path, branch: str, transition: str, inspection: str) -> Mark | None:
    """Where this inspection last read this branch for this transition, or None."""
    entry = _load(where).get(_key(branch, transition, inspection))
    if not isinstance(entry, dict):
        return None
    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return None
    tree, head = entry.get("tree"), entry.get("head")
    if not isinstance(tree, str) or not isinstance(head, str) or not tree:
        return None
    return Mark(
        at=at,
        head=head,
        tree=tree,
        base=str(entry.get("base") or ""),
        given=str(entry.get("given") or ""),
        answer=str(entry.get("answer") or ""),
    )


def keep(where: Path, branch: str, transition: str, inspection: str, mark: Mark) -> None:
    """Move this inspection's mark. Best-effort: a mark that cannot be written
    leaves the next share wider, and costs nothing else."""
    noted = _load(where)
    noted[_key(branch, transition, inspection)] = {
        "at": mark.at.isoformat(),
        "head": mark.head,
        "tree": mark.tree,
        "base": mark.base,
        "given": mark.given,
        "answer": mark.answer[:ANSWER_LIMIT],
    }
    if len(noted) > MARKS:
        # Oldest out, by the time written into each.
        newest = sorted(noted.items(), key=lambda item: str(item[1].get("at") or ""), reverse=True)
        noted = dict(newest[:MARKS])
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(noted), encoding="utf-8")
    except OSError as unwritable:
        logger.warning("Could not keep where %s last read %s: %s", inspection, branch, unwritable)


def _when(at: datetime) -> str:
    local = at.astimezone()
    return f"{local:%b} {local.day} {local:%H:%M}"


def _short(ident: str) -> str:
    return ident[: frame.SHOWN]


def base_line(project: Path, found: frame.Base | None, now: frame.Tree) -> str:
    """The branch since its base, for every transition's envelope."""
    if found is None:
        return "Base: none found — no `base:` set, no origin/HEAD, and no main or master"
    commits = f"{found.commits} commit{'' if found.commits == 1 else 's'} since"
    since = frame.changed(project, found.commit, now.content_id)
    return (
        f"Base: {found.ref} at {_short(found.commit)} · {commits} · {since} — "
        f"git diff {_short(found.commit)} {now.content}"
    )


def share(project: Path, mark: Mark | None, *, base: frame.Base | None, now: frame.Tree) -> Share:
    """One inspection's share: what changed since its mark, or the base's range
    when it has none here — said, so a share as wide as the branch is not
    taken for a small one."""
    from_base = f"git diff {_short(base.commit)} {now.content}" if base is not None else ""
    if mark is None:
        whole = "its share is the base's range" if base else "no base was found either"
        return Share(
            f"{SINCE}: it has not, on this branch on this machine — {whole}", from_base, ""
        )
    when = _when(mark.at)
    earlier = (
        f"This inspection's last answer here, at {when}"
        + (f", on {mark.given}" if mark.given else "")
        + f":\n\n{mark.answer}"
        if mark.answer
        else ""
    )
    if frame.kept(project, mark.tree):
        start, how = mark.tree, ""
    elif frame.kept(project, mark.head):
        start, how = mark.head, ", whose files are gone, so from its HEAD then"
    else:
        whole = "its share is the base's range" if base else "no base was found either"
        return Share(f"{SINCE}: {when}, of which nothing is left — {whole}", from_base, earlier)
    merged = (
        " · the base was merged in since, so this includes the base's own changes"
        if base is not None and mark.base and mark.base != base.commit
        else ""
    )
    given = f"git diff {_short(start)} {now.content}"
    since = frame.changed(project, start, now.content_id)
    return Share(f"{SINCE}: {when}{how} · {since}{merged} — {given}", given, earlier)
