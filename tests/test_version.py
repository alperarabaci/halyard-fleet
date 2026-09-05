"""Tests that the two places holding the version agree.

`src/halyard/__init__.py` said `0.1.0` through six releases. Nothing imported
it, so nothing could notice — which is the shape of every fact kept in more than
one place: the copy nobody reads is the copy that rots, and it rots quietly.

Holding them together in a test rather than reducing them to one copy is a
decision with measurements behind it, written down beside the literal itself.
The short version: both single-copy arrangements move the number somewhere it
can be stale about the code that is actually running, and a wrong number is
worse than a second copy that CI refuses to let drift.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import halyard
from halyard.config import ChannelKind, Settings

REPO = Path(__file__).resolve().parent.parent


def declared() -> str:
    """What the package says it is, read from the file rather than the install.

    Not `importlib.metadata.version` — an editable install keeps the version it
    was installed with, so after a bump this would compare the literal against a
    stale copy of itself and fail on a machine where nothing is wrong.
    """
    written = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return written["project"]["version"]


def test_the_module_and_the_package_say_the_same_thing() -> None:
    """The whole point. A release edits both, and forgetting one fails here."""
    assert halyard.__version__ == declared()


def test_the_application_reports_it_rather_than_a_literal_of_its_own() -> None:
    """`create_app` held a third copy, hand-edited alongside the other two and
    checked by nobody. It is what `/openapi.json` answers with.
    """
    from halyard.api.app import create_app

    app = create_app(
        Settings(
            HALYARD_CHANNEL=ChannelKind.STUB_ALLOW.value,
            HALYARD_DB_PATH="/dev/null",
            HALYARD_AUDIT_LOG="/dev/null",
            _env_file=None,
        )
    )

    assert app.version == halyard.__version__


def test_the_lockfile_carries_the_version_too() -> None:
    """`uv lock` writes it, so this catches a bump where the lock was not
    regenerated — the third file in the release, and the one with no reason to
    be opened by hand.
    """
    locked = (REPO / "uv.lock").read_text(encoding="utf-8")
    entry = locked.split('name = "halyard-fleet"', 1)[1].split("[[package]]", 1)[0]

    assert f'version = "{declared()}"' in entry
