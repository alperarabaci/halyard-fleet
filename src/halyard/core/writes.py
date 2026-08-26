"""Paths the agent may write to without being asked.

Everything else about this project is arranged so that nothing is approved
without a person. This is the one place that grants, and it exists because of a
failure the gate could not see: a turn started from a phone runs headless, and
Claude Code cannot open a permission dialog there — so a `Write` that is not on
its own allow list is *denied outright*, with no card, no question and nothing
on the phone to answer. Work stopped mid-sentence and the reason was invisible.

The fix has two halves and this is the second one. The gate now covers `Write`
and `Edit`, so an unlisted write asks instead of failing. That alone would put a
card on the phone for every file an agent touches, so a person can name the
places where that question is not worth asking:

    writes:
      - NOTES/**

**A grant is not a guess.** Every rule below exists because getting this wrong
hands an agent write access somebody did not intend:

- The default is empty. Nothing is pre-authorized by forgetting to configure.
- A pattern only ever grants *inside the project the write belongs to*. It is
  matched against the path relative to that project, so `NOTES/**` cannot be
  talked into meaning `/etc`.
- The path is resolved first, symlinks and `..` included, and a path that lands
  outside the project after resolving is refused however it was spelled. That is
  the traversal this would otherwise be wide open to: `NOTES/../../../etc/passwd`
  starts with `NOTES/` and is not in `NOTES`.
- `*` stops at a directory separator and `**` crosses them, which is what people
  already mean by those. A `*` that quietly spanned directories would grant a
  subtree somebody thought they had excluded.
- Every grant is written to the audit log with the pattern that allowed it, so
  "why did that run without asking" always has an answer.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

#: The tools this applies to: the ones that take a `file_path` and change it.
#: Kept here rather than in the gate's matcher because these two answer
#: different questions — the matcher decides what Halyard is *shown*, this
#: decides what it may let through without a person.
FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one glob into a regex with the separator rules people expect.

    `fnmatch` is not usable here: its `*` matches `/` as well, so `NOTES/*`
    would grant every level beneath `NOTES` to somebody who wrote the narrower
    form on purpose. A grant that is wider than it reads is the thing this
    module exists to avoid.
    """
    out = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 3] == "**/":
                # `**/` also matches nothing, so `**/x.md` covers a bare `x.md`.
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _inside(path: Path, project: Path) -> PurePosixPath | None:
    """Where `path` sits inside `project`, or None if it does not sit in it.

    Both are resolved before they are compared, so a symlink pointing out of the
    tree and a `..` climbing out of it are the same refusal. Resolving a file
    that does not exist yet is fine and is the normal case here — the agent is
    about to create it.
    """
    try:
        root = Path(project).expanduser().resolve()
        here = Path(path).expanduser()
        # A relative path is measured from the project, never from wherever the
        # control plane happens to be running. Its working directory is a fact
        # about the service, not about the write, and resolving against it would
        # make the same path mean different things on different machines.
        resolved = (here if here.is_absolute() else root / here).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        return PurePosixPath(resolved.relative_to(root).as_posix())
    except ValueError:
        return None


def allowed_by(
    file_path: str | None, project_dir: str | None, patterns: tuple[str, ...]
) -> str | None:
    """The pattern that pre-authorizes this write, or None to go and ask.

    None is the answer to everything uncertain: no path, no project to measure
    it against, a path outside that project, or simply nothing matching. The
    caller turns None into a card on somebody's phone, which is the behaviour
    this whole system is built around — so being wrong here costs a question,
    never a silent grant.
    """
    if not file_path or not project_dir or not patterns:
        return None

    relative = _inside(Path(file_path), Path(project_dir))
    if relative is None:
        return None

    text = relative.as_posix()
    for pattern in patterns:
        cleaned = pattern.strip().strip("/")
        if not cleaned:
            continue
        if _to_regex(cleaned).match(text):
            return pattern
        # A bare directory name means everything under it, which is what
        # somebody writing `NOTES` rather than `NOTES/**` has in mind.
        if not any(char in cleaned for char in "*?") and text.startswith(f"{cleaned}/"):
            return pattern
    return None


def from_yaml(text: str) -> tuple[str, ...]:
    """The `writes:` block: paths that may be written without asking.

    Refuses a shape it cannot honour rather than ignoring it. A grant somebody
    believes they have written and that is silently not there is the worse of
    the two failures — they would leave the desk expecting it to work.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None

    block: Any = loaded.get("writes") if isinstance(loaded, dict) else None
    if block is None:
        return ()
    if not isinstance(block, list):
        raise ValueError("`writes:` must be a list of path patterns, one per line.")

    patterns: list[str] = []
    for entry in block:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("Every entry under `writes:` must be a path pattern, and not empty.")
        cleaned = entry.strip()
        if cleaned.startswith("/") or cleaned.startswith("~"):
            # An absolute pattern reads as though it grants that path anywhere,
            # and it never can — patterns are matched inside the project a write
            # belongs to. Refused rather than quietly meaning something else.
            raise ValueError(
                f"`writes:` entry {entry!r} is an absolute path. Patterns are matched "
                "inside the project the write belongs to, so write it relative to that."
            )
        patterns.append(cleaned)
    return tuple(patterns)


def load(directory: Path | None = None) -> tuple[str, ...]:
    """Patterns from the configuration, or none at all if there is no file."""
    from halyard.core.config_file import find_config

    path = find_config(directory)
    if path is None:
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    try:
        return from_yaml(text)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
