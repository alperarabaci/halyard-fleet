"""A project's own checks, run over the last thing an agent said.

Some failures no guard catches. A claim made with no evidence behind it, a
change that quietly deletes or hides something, a gate reported green that
nobody ran: a rule matching words cannot tell any of these from the honest
version, and a model reading the text can. So a project writes each check as a
file — what to look for, and how to answer — and `/checks` offers them as
buttons: the one pressed goes in front of a model with the chat's last reply.

The files belong to the project, for the reason `confirmation:` files do: what
is worth checking is something a team learns about its own failures. Halyard
reads them, adds what it can see for itself — the task the branch is for, and
where the repository stands — and hands the answer back as it came.

**The turn cannot look for itself.** It runs apart from the project, so it sees
only what it is given and can neither open a file nor run a command. The prompt
says so, which keeps a check that needed a command run from answering as though
it had run one.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from halyard.tasks.branches import current, number_of

logger = logging.getLogger(__name__)

#: A check is a page of instructions. Past this the file is wrong, not long.
LIMIT = 40_000

#: Reading a repository is local, and should never take long.
GIT_TIMEOUT = 10.0


def read(path: Path, project: Path | None) -> str:
    """The check's own text, or nothing if it cannot be read.

    Relative to the project, which is where these files live. A missing one is
    said by the caller as a check that did not run, and by `doctor` before that.
    """
    wanted = path.expanduser()
    if not wanted.is_absolute() and project is not None:
        wanted = project / wanted
    try:
        text = wanted.read_text(encoding="utf-8").strip()
    except OSError as missing:
        logger.warning("Could not read the check %s: %s", wanted, missing)
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
    changed on top of it — because a check of a report is only good for the
    tree that report was about.
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
    """Which revision of a check ran: the commit that last changed its file, and
    whether it has been edited since.

    What a finding was a finding *by*, once the file changes next week.
    """
    commit = _git(project, "log", "-1", "--format=%h", "--", str(path))
    if not commit:
        return "uncommitted"
    edited = _git(project, "status", "--porcelain", "--", str(path))
    return f"{commit} + local edits" if edited else commit


def prompt(instructions: str, *, context: list[str], note: str, text: str) -> str:
    """One check's turn: its own instructions, what Halyard knows, and the text."""
    parts = [
        instructions,
        "",
        "---",
        "",
        "This check runs apart from the project: it cannot open files or run "
        "commands, so judge only what is written here.",
        "",
        *context,
    ]
    if note:
        parts.append(f"Said by whoever asked: {note}")
    parts += ["", "Text to check:", "", text]
    return "\n".join(parts)


def handed_on(
    name: str,
    *,
    path: Path,
    version: str,
    author: str,
    arrived: str,
    context: list[str],
    findings: str,
    reply: str,
) -> str:
    """What a seat is handed when somebody sends it a check's answer.

    Written to be read cold by a session that saw none of it happen: what ran,
    on whose reply, where, what it found — and the reply itself, because a
    finding about a report the reader does not have is a finding nobody can
    weigh. Measured: a navigator handed the findings alone could not tell what
    they were about.
    """
    return "\n".join(
        [
            f"The operator ran this project's `{name}` check on {author}'s reply "
            f"from {arrived} and is handing you the result: what the check found, "
            "then the reply it checked.",
            "",
            f"Check: {name} — {path} @ {version}",
            f"Where: {' · '.join(context)}",
            "",
            "What it found:",
            "",
            findings,
            "",
            f"The reply it checked ({len(reply):,} characters):",
            "",
            reply,
        ]
    )


def unfenced(answer: str) -> str:
    """An answer wrapped whole in a code fence, without the fence.

    It is shown in `<pre>` already, where a fence would print as backticks.
    """
    lines = answer.strip().splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return answer.strip()
