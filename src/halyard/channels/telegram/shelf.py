"""The cards still open, kept on disk, so a restart can close them.

The desk keeps its cards in memory, which a restart empties. Every card that
was open then stayed open in the chat — buttons and all — answering anything
pressed on it with "no longer open", until somebody scrolled past it. So each
card is written here as it opens and struck off as it closes, and what is
still here when Halyard starts is what the last process left open.

Never raises. A card that cannot be kept costs its closing after a restart,
never the card itself or anything it asks.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS open_cards (
    chat        TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    html        TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    PRIMARY KEY (chat, message_id)
);
"""


@dataclass(frozen=True)
class Kept:
    """A card that was open when it was last written down."""

    chat: str
    message_id: int
    kind: str
    key: str
    #: What it said, so closing it keeps that.
    html: str
    opened_at: datetime


class Shelf:
    """The open cards, in a table of the database Halyard keeps."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def put(self, kept: Kept) -> None:
        """Write a card down as open."""
        self._write(
            "INSERT OR REPLACE INTO open_cards (chat, message_id, kind, key, html, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                kept.chat,
                kept.message_id,
                kept.kind,
                kept.key,
                kept.html,
                kept.opened_at.isoformat(),
            ),
        )

    def take(self, chat: str, message_id: int) -> None:
        """Strike a card off: it is closed."""
        self._write("DELETE FROM open_cards WHERE chat = ? AND message_id = ?", (chat, message_id))

    def everything(self) -> list[Kept]:
        """Every card written down as open, oldest first."""
        if not self._path.is_file():
            return []
        try:
            with contextlib.closing(sqlite3.connect(self._path)) as db:
                db.executescript(_SCHEMA)
                rows = db.execute(
                    "SELECT chat, message_id, kind, key, html, opened_at FROM open_cards "
                    "ORDER BY opened_at"
                ).fetchall()
        except sqlite3.Error:
            logger.warning("Could not read the open cards from %s", self._path, exc_info=True)
            return []
        kept = []
        for chat, message_id, kind, key, html, opened_at in rows:
            try:
                kept.append(
                    Kept(chat, int(message_id), kind, key, html, datetime.fromisoformat(opened_at))
                )
            except (TypeError, ValueError):
                continue
        return kept

    def _write(self, sql: str, params: tuple) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(self._path)) as db:
                db.executescript(_SCHEMA)
                db.execute(sql, params)
                db.commit()
        except (OSError, sqlite3.Error):
            logger.warning("Could not keep an open card in %s", self._path, exc_info=True)
