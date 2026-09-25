"""ZCode's sessions, by the titles its application gives them.

A hook call carries no name, only a `sess_<uuid>`. The title a session shows in
ZCode's own list is in the application's database, `~/.zcode/cli/db/db.sqlite`,
table `session` — found there by ZCode itself on 3.11.2, and the schema read
the same day. So a ZCode seat names its session the way a Claude Code seat
does, and `doctor` finds it where it could only fail before.

**Titles, not prompts.** Until a title exists ZCode shows a session under the
first thing somebody typed into it (`title_source` `first_input`), which is a
prompt rather than a name. A title ZCode generated is taken and said to be
generated — it moves as the work goes on — and one set by hand wins.

**Read-only, and quiet.** The database is the application's, not a file meant
for anybody else: it is opened read-only, and a missing file or a schema that
has moved answers nothing rather than raising. A seat without a name is still
found by its project.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from halyard.agents.base import SessionRef

#: Under the home directory.
DATABASE = Path(".zcode") / "cli" / "db" / "db.sqlite"

#: The `title_source` values that are names. The third, `first_input`, is a prompt.
NAMED = ("custom", "generated")

#: Sessions a seat can mean: not a subagent's, which is a child of the session
#: that started it, and not one put away.
_QUERY = (
    "SELECT id, title, title_source, directory, time_created, time_updated FROM session "
    "WHERE parent_id IS NULL AND time_archived IS NULL AND title_source IN (?, ?) "
    "ORDER BY time_updated DESC"
)


def _home() -> Path:
    return Path.home()


def _moment(value: object) -> datetime | None:
    """One of ZCode's timestamps — milliseconds, as opencode keeps them — as a time."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    seconds = value / 1000 if value > 10**11 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _sessions(home: Path | None) -> list[SessionRef]:
    """Every titled session a seat could mean, newest first."""
    database = (home or _home()) / DATABASE
    if not database.is_file():
        return []
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as db:
            rows = db.execute(_QUERY, NAMED).fetchall()
    except sqlite3.Error:
        return []
    return [
        SessionRef(
            session_id=str(session_id),
            name=str(title),
            cwd=str(directory) if directory else None,
            named_by_a_person=source == "custom",
            started_at=_moment(created),
            last_active=_moment(updated),
        )
        for session_id, title, source, directory, created, updated in rows
        if title
    ]


def find_session(name: str, home: Path | None = None) -> SessionRef | None:
    """The session with this title, or this id.

    The newest, and a title set by hand before one ZCode generated: two
    sessions can share a generated title, and the one somebody named on
    purpose is the one a seat means.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    matching = [
        ref
        for ref in _sessions(home)
        if wanted in (ref.session_id.casefold(), ref.name.strip().casefold())
    ]
    # Stable, so within each kind the newest stays first.
    matching.sort(key=lambda ref: not ref.named_by_a_person)
    return matching[0] if matching else None


def list_sessions(home: Path | None = None) -> list[SessionRef]:
    """Every titled session, newest first."""
    return _sessions(home)
