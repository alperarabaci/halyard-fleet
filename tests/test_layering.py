"""A tripwire: inspections and handoffs stay apart from what uses them.

Two pieces, one direction between them. A handoff may run inspections; an
inspection never hands anything on. Neither knows a chat or a runtime: they
reach a model through `inspections.Asker` and a seat through
`handoffs.Delivery`, and the Telegram channel is only the one adapter that
answers both today.

That holds only while nothing imports across it, and it goes quietly when it
goes — one convenient import from the channel, and the next channel has to be
the first one again. This reads the source and says which import crossed.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "halyard"


def _imports(path: Path) -> set[str]:
    """Every module this file imports from, by its dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _package(name: str) -> list[Path]:
    return sorted((SOURCE / name).rglob("*.py"))


def _crossings(files: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(SOURCE)} imports {module}"
        for path in files
        for module in sorted(_imports(path))
        if any(module == name or module.startswith(f"{name}.") for name in forbidden)
    ]


def test_inspections_and_handoffs_know_no_chat_and_no_runtime() -> None:
    """A model through `Asker`, a seat through `Delivery`, and nothing else."""
    packages = ("inspections", "handoffs", "workflows")
    # Each package, not their sum. When `checks` became `inspections`, this
    # went on reading a folder that was gone, and the other two kept it passing.
    missing = [name for name in packages if not _package(name)]
    assert not missing, f"{missing} not found, so this proves nothing about them"
    files = [*(path for name in packages for path in _package(name)), SOURCE / "frame.py"]

    crossings = _crossings(files, ("halyard.channels", "halyard.agents"))

    assert not crossings, (
        "An inspection or a handoff reaches past its ports:\n  "
        + "\n  ".join(crossings)
        + "\n\nAsk for what is needed through `inspections.Asker` or `handoffs.Delivery`, "
        "and let the channel answer it."
    )


def test_an_inspection_never_hands_anything_on() -> None:
    """The one direction: handoffs use inspections, never the other way round."""
    files = [*_package("inspections"), SOURCE / "frame.py"]
    assert len(files) > 1, "inspections not found, so this proves nothing"

    crossings = _crossings(files, ("halyard.handoffs",))

    assert not crossings, "An inspection imports a handoff:\n  " + "\n  ".join(crossings)
