"""A tripwire: no runtime may be named outside its own package.

`RuntimeSpec` exists so that adding a runtime is adding a package. That holds
only while nothing else knows a runtime by name — the moment core says
`if agent_id == "claude-code"`, the next runtime needs a change there too, and
then in the file after that, and the promise is quietly gone.

It went quietly once already. The transcript watcher was written with
`CLAUDE_CODE = "claude-code"` and a Claude-shaped parser in `core/`, and it
worked perfectly until Codex needed the same thing with a different filename
and a different entry shape. Nobody noticed, because nothing was watching.

This is what watches. It reads the source rather than trusting a convention,
and it fails on the name that leaked and says where.

Comments are invisible to it — they are not in the tree — and docstrings are
skipped on purpose: prose that *explains* a runtime is how this codebase
records what it measured, and forbidding that would cost more than it saves.
What is caught is a name a branch could be taken on.
"""

from __future__ import annotations

import ast
from pathlib import Path

from halyard.agents import registry

SOURCE = Path(__file__).resolve().parent.parent / "src" / "halyard"

#: Where a runtime is allowed to know its own name.
OWNED = SOURCE / "agents"

#: Directories a runtime keeps its own files in. Named here so the check can
#: catch the other way this leaks: not the runtime's name, but the path only
#: that runtime uses.
HOMES = (".claude", ".codex", ".gemini", ".antigravity")


def _docstrings(tree: ast.AST) -> set[int]:
    """Every string node that is a docstring, by identity."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _named_runtimes(path: Path, names: set[str]) -> list[tuple[int, str]]:
    """Runtime names used as values in this file, with their line numbers."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    skip = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        if isinstance(node.value, str) and node.value in names:
            found.append((node.lineno, node.value))
    return found


def test_no_module_outside_the_agents_packages_names_a_runtime() -> None:
    """Adding a runtime should be adding a package, and this is what makes that
    true rather than aspirational."""
    names = set(registry.names())
    assert names, "the registry found no runtimes, so this check proves nothing"

    leaks = [
        f"{path.relative_to(SOURCE)}:{line} names {name!r}"
        for path in SOURCE.rglob("*.py")
        if OWNED not in path.parents
        for line, name in _named_runtimes(path, names)
    ]

    assert not leaks, (
        "A runtime is named outside its own package:\n  "
        + "\n  ".join(leaks)
        + "\n\nPut what differs on `RuntimeSpec` and let the registry answer, "
        "the way `hooks`, `verify` and `watching` already do."
    )


def test_no_module_outside_the_agents_packages_knows_where_one_lives() -> None:
    """The other way it leaks: not the name, but the directory only that
    runtime uses. `~/.claude` in core is the same coupling spelled differently."""
    leaks = [
        f"{path.relative_to(SOURCE)}:{line} uses {home!r}"
        for path in SOURCE.rglob("*.py")
        if OWNED not in path.parents
        for line, home in _named_runtimes(path, set(HOMES))
    ]

    assert not leaks, (
        "A runtime's own directory is named outside its package:\n  "
        + "\n  ".join(leaks)
        + "\n\n`Watching.home` on the spec is where this belongs."
    )
