"""Where the running log goes, and how it is broken into readable pieces.

One file grew to thirty-six thousand lines in a month, which is not a log
anybody reads — it is a file you grep and hope. The unit somebody actually
thinks in is the week: *what happened last Tuesday*, *it started going wrong
around the 20th*. So the log rotates weekly, into a folder of its own, and the
week is in the filename rather than something you work out from timestamps.

    logs/halyard.log              this week
    logs/halyard.log.2026-W34     the one before it

**Size still bounds it.** Weekly on its own would mean one bad week — a tight
error loop, a runtime failing every second — could fill a disk before the next
Monday, and this is a service that runs for months unattended. So the handler
below rolls on *either* condition, and a second roll inside one week keeps both
halves instead of overwriting the first.

The bridge log is not written from here and cannot use this handler at all. It
is appended to by every hook process the runtimes start — several at once, each
alive for under a second — and rotation by renaming is a race when writers are
concurrent. It splits by writing to a *week-stamped name* instead, so no file
is ever renamed and no two processes need to agree on anything; see
`bridge/_settings.py`. What is left here is the pruning, which the control plane
does at startup because it is the one process that is alone.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

#: `2026-W35`. ISO, so the week number agrees with the year beside it even in
#: the days around New Year, where the calendar year and the ISO year differ.
WEEK_SUFFIX = "%G-W%V"

#: What a bridge log is called for a given week. Kept here as the one place the
#: shape is written down, even though the bridge builds its own copy — those
#: scripts run with no virtualenv and cannot import this.
BRIDGE_STEM = "bridge"

_BRIDGE_FILE = re.compile(rf"^{BRIDGE_STEM}-\d{{4}}-W\d{{2}}\.log$")


def week_stamp(when: datetime | None = None) -> str:
    """This week, as it appears in a filename."""
    return (when or datetime.now()).strftime(WEEK_SUFFIX)


class WeeklyFileHandler(TimedRotatingFileHandler):
    """Rolls every Monday, and again if one week gets enormous.

    `TimedRotatingFileHandler` alone would delete the week's first file when a
    size roll produced the same dated name a second time. That is a log quietly
    eating itself, which is the shape of bug this project keeps writing down, so
    a size roll gets the time appended and both halves survive.

    Pruning is done here rather than inherited for the same reason: the parent
    matches rotated files against a pattern built from `suffix`, and would not
    recognise — or ever delete — the ones a size roll produced.
    """

    def __init__(self, filename: Path, *, backup_count: int, max_bytes: int) -> None:
        super().__init__(
            str(filename),
            when="W0",
            backupCount=backup_count,
            encoding="utf-8",
        )
        self.suffix = WEEK_SUFFIX
        self._max_bytes = max(0, max_bytes)
        self._rolling_on_size = False

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802 — the parent's
        if super().shouldRollover(record):
            self._rolling_on_size = False
            return 1
        if self._max_bytes and self.stream is not None:
            self.stream.seek(0, 2)
            if self.stream.tell() >= self._max_bytes:
                self._rolling_on_size = True
                return 1
        return 0

    def rotation_filename(self, default_name: str) -> str:
        if not self._rolling_on_size:
            return default_name
        # Two rolls in one week would otherwise land on one name, and the
        # parent's answer to that is to delete what is already there.
        #
        # The time alone is not enough, which a test caught rather than a
        # reviewer: a loop noisy enough to trip the ceiling trips it several
        # times inside one second, and second-resolution names collided — so
        # the file this exists to save was deleted by the roll after it. Counted
        # up to a free name instead, which cannot collide at any rate.
        stem = f"{default_name}-{datetime.now().strftime('%H%M%S')}"
        candidate, index = stem, 1
        while Path(candidate).exists():
            candidate = f"{stem}-{index}"
            index += 1
        return candidate

    def getFilesToDelete(self) -> list[str]:  # noqa: N802 — the parent's spelling
        """Every rotated file but the newest `backupCount`, by age on disk."""
        if self.backupCount <= 0:
            return []
        base = Path(self.baseFilename)
        rotated = sorted(
            (path for path in base.parent.glob(f"{base.name}.*") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        surplus = len(rotated) - self.backupCount
        return [str(path) for path in rotated[:surplus]] if surplus > 0 else []


def prune_bridge_logs(directory: Path, keep: int) -> list[Path]:
    """Drop all but the newest `keep` weekly bridge logs.

    Done by the control plane at startup, because it is the only process in this
    system that is alone. The hooks that write those files run several at a time
    and for less than a second each; having them delete each other's files would
    be a race in exchange for nothing.

    Returns what it removed, and never raises: a log that cannot be tidied is
    not a reason to stop serving.
    """
    if keep <= 0:
        return []
    try:
        found = directory.glob(f"{BRIDGE_STEM}-*.log")
        # By name, which for an ISO week stamp sorts chronologically.
        weekly = sorted(
            (path for path in found if _BRIDGE_FILE.match(path.name)),
            key=lambda path: path.name,
        )
    except OSError:
        return []

    removed: list[Path] = []
    for path in weekly[: max(0, len(weekly) - keep)]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            logger.debug("Could not remove the old bridge log %s", path, exc_info=True)
    return removed
