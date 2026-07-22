"""What the log keeps, and what it must never keep.

A control plane runs for days with nobody watching the terminal. The questions
worth asking about it are asked afterwards — why was that denied, was anything
even running — and a log that only ever existed on a screen cannot answer them.

The second half of this file matters more than the first. Writing logs to a
file changed what a leaked credential costs: it used to scroll past, and now it
stays on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from halyard.__main__ import configure_logging
from halyard.config import Settings


def teardown_function() -> None:
    """Put the root logger back, so one test cannot configure another's."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def test_the_log_is_written_to_a_file_with_timestamps(tmp_path: Path) -> None:
    destination = tmp_path / "halyard.log"

    configure_logging(level="INFO", log_file=destination)
    logging.getLogger("halyard").info("the control plane started")

    written = destination.read_text()
    assert "the control plane started" in written
    # A date, so two runs can be told apart afterwards.
    assert "INFO" in written
    assert written[:2].isdigit()


def test_the_level_can_be_turned_down(tmp_path: Path) -> None:
    destination = tmp_path / "halyard.log"

    configure_logging(level="WARNING", log_file=destination)
    logging.getLogger("halyard").info("routine")
    logging.getLogger("halyard").warning("something to look at")

    written = destination.read_text()
    assert "routine" not in written
    assert "something to look at" in written


def test_debug_reaches_the_file(tmp_path: Path) -> None:
    destination = tmp_path / "halyard.log"

    configure_logging(level="DEBUG", log_file=destination)
    logging.getLogger("halyard").debug("the detail you turned this on for")

    assert "the detail you turned this on for" in destination.read_text()


def test_a_bot_token_never_reaches_the_file(tmp_path: Path) -> None:
    """The reason this file exists.

    The known leak was httpx logging a request line with the token in the URL
    path. The filter goes on every handler rather than on the root logger,
    because a filter on a logger does not see records that propagate up to it —
    get that wrong and the console is clean while the file on disk is not.
    """
    destination = tmp_path / "halyard.log"
    secret = "8123456789:AAH1234567890abcdefghijklmnopqrstuvw"

    configure_logging(level="INFO", log_file=destination)
    logging.getLogger("halyard").warning(
        "polling https://api.telegram.org/bot%s/getUpdates", secret
    )
    logging.getLogger("halyard").warning("bare token %s", secret)

    written = destination.read_text()
    assert "AAH1234567890abcdefghijklmnopqrstuvw" not in written
    # The bot id survives: it says which bot without granting anything.
    assert "8123456789" in written


def test_logging_still_starts_when_the_file_cannot_be_opened(tmp_path: Path) -> None:
    """Losing the log is bad. Refusing to run the gate over it would be worse."""
    impossible = tmp_path / "not-a-directory" / "deeper"
    impossible.parent.write_text("this is a file, not a directory")

    configure_logging(level="INFO", log_file=impossible / "halyard.log")
    logging.getLogger("halyard").info("still running")

    assert logging.getLogger().handlers


def test_an_unknown_level_is_refused_rather_than_quietly_ignored(monkeypatch) -> None:
    """A typo would otherwise leave you reading an INFO log and trusting it.

    Which is the worst shape for this mistake: you turn on debugging, see
    nothing unusual, and conclude nothing unusual happened.
    """
    monkeypatch.setenv("HALYARD_CHANNEL", "stub_deny")
    monkeypatch.setenv("HALYARD_LOG_LEVEL", "DEBUGG")

    with pytest.raises(ValueError, match="is not a level"):
        Settings()


def test_the_file_can_be_turned_off(monkeypatch) -> None:
    monkeypatch.setenv("HALYARD_CHANNEL", "stub_deny")
    monkeypatch.setenv("HALYARD_LOG_FILE", "")

    assert Settings().log_file is None


def test_a_file_is_kept_by_default(monkeypatch) -> None:
    """On unless turned off: the moment you want it is always in the past."""
    monkeypatch.setenv("HALYARD_CHANNEL", "stub_deny")

    assert Settings().log_file is not None
