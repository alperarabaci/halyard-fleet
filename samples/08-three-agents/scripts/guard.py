"""The boundary guard: what each context may use of the others, checked.

Each context is a package. Another may use it only through its public face —
`import catalog`, and only what `catalog/__init__.py` lists in `__all__` — and
only if `USES` below allows it. Reaching inside one, or using one it may not,
is a crossing, named with its file and line.

Its last line is the one Halyard reads, and it exits 0 when every boundary is
kept, 1 on a crossing, and 2 when it could not look at all.
"""

import ast
import sys
from pathlib import Path

#: Each context, and the contexts it may use, through their public face only.
USES: dict[str, set[str]] = {
    "catalog": set(),
    "cart": {"catalog"},
}

ROOT = Path(__file__).resolve().parent.parent


def public_face(context: str) -> set[str]:
    """What `context` offers: the names its `__init__.py` lists in `__all__`."""
    face = Path(context, "__init__.py")
    if not (ROOT / face).is_file():
        raise ValueError(f"{face} is not there")
    tree = ast.parse((ROOT / face).read_text(encoding="utf-8"), str(face))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise ValueError(f"{context}/__init__.py lists no __all__, so it offers nothing to check")


def crossings(context: str, path: Path, faces: dict[str, set[str]]) -> list[str]:
    """Each place in `path` where `context` crosses a boundary."""
    found: list[str] = []
    here = path.relative_to(ROOT)
    package = here.parent.parts
    bound: dict[str, str] = {}  # a name in this file, and the context it stands for

    def cross(line: int, why: str) -> None:
        found.append(f"{here}:{line} {why}")

    def uses(line: int, other: str) -> bool:
        if other == context or other not in USES:
            return False
        if other not in USES[context]:
            cross(line, f"uses {other}, which {context} may not")
            return False
        return True

    tree = ast.parse(path.read_text(encoding="utf-8"), str(here))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                other, _, inside = alias.name.partition(".")
                if uses(node.lineno, other):
                    if inside:
                        cross(node.lineno, f"reaches into {alias.name}; use {other}")
                    else:
                        bound[alias.asname or other] = other
        elif isinstance(node, ast.ImportFrom):
            parts = [*package[: len(package) - node.level + 1]] if node.level else []
            name = ".".join([*parts, *([node.module] if node.module else [])])
            if not name:
                # `from .. import cart`: each name is a context of its own.
                for alias in node.names:
                    if uses(node.lineno, alias.name):
                        bound[alias.asname or alias.name] = alias.name
                continue
            other, _, inside = name.partition(".")
            if not uses(node.lineno, other):
                continue
            if inside:
                cross(node.lineno, f"reaches into {name}; use {other}")
                continue
            for alias in node.names:
                if alias.name not in faces[other]:
                    cross(node.lineno, f"takes {other}.{alias.name}, which {other} does not offer")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bound
            and node.attr not in faces[bound[node.value.id]]
        ):
            other = bound[node.value.id]
            cross(node.lineno, f"reaches into {other}.{node.attr}, which {other} does not offer")
    return found


def main() -> int:
    try:
        faces = {context: public_face(context) for context in USES}
        files = {context: sorted((ROOT / context).rglob("*.py")) for context in USES}
        found = [
            crossing
            for context, paths in files.items()
            for path in paths
            for crossing in crossings(context, path, faces)
        ]
    except SyntaxError as why:
        print(f"could not look: {why.filename}:{why.lineno} does not parse")
        return 2
    except (OSError, ValueError) as why:
        print(f"could not look: {why}")
        return 2
    for crossing in found:
        print(crossing)
    if found:
        more = f" (and {len(found) - 1} more, above)" if len(found) > 1 else ""
        print(f"{len(found)} crossing{'s' if len(found) > 1 else ''}: {found[0]}{more}")
        return 1
    print(f"every boundary kept ({len(USES)} contexts, {sum(map(len, files.values()))} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
