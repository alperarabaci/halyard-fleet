"""Tests for how old the control plane's own credential is getting.

The awkward part of this is that nothing can be checked against a truth. There
is no expiry to read — `claude auth status` answers with eight fields and not
one of them is a date — so what is tested is that the estimate is honest about
being one, that the clock starts when it should and not before, and that the
token itself is never written down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.core import credentials

TOKEN = "sk-ant-oat01-not-a-real-one"
NOW = datetime(2026, 9, 5, tzinfo=UTC)


def test_the_clock_starts_the_first_time_a_token_is_seen(tmp_path: Path) -> None:
    aged = credentials.remember(TOKEN, tmp_path / "seen.json", now=NOW)

    assert aged is not None
    assert aged.first_seen == NOW
    assert aged.expected == NOW + credentials.ASSUMED_LIFE


def test_starting_again_does_not_restart_it(tmp_path: Path) -> None:
    """The whole value is in the date being older than this run."""
    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)

    later = credentials.remember(TOKEN, where, now=NOW + timedelta(days=200))

    assert later is not None
    assert later.first_seen == NOW
    assert later.days_left(NOW + timedelta(days=200)) == 165


def test_a_new_token_starts_a_new_clock(tmp_path: Path) -> None:
    """Minting a replacement is how somebody answers this warning, so it has to
    stop the warning without anybody clearing anything by hand."""
    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)

    replaced = credentials.remember(
        "sk-ant-oat01-a-fresh-one", where, now=NOW + timedelta(days=400)
    )

    assert replaced is not None
    assert replaced.first_seen == NOW + timedelta(days=400)
    assert not replaced.worth_saying(NOW + timedelta(days=400))


def test_the_token_is_never_written_down(tmp_path: Path) -> None:
    """A fingerprint is enough to tell "still the same one" from "somebody
    replaced it", and is not enough to be anything else."""
    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)

    written = where.read_text()
    noted = json.loads(written)

    assert TOKEN not in written
    assert "oat01" not in written
    assert set(noted) == {"salt", "fingerprint", "first_seen"}
    assert noted["fingerprint"] == credentials.fingerprint(TOKEN, noted["salt"])


def test_only_the_token_in_use_is_remembered(tmp_path: Path) -> None:
    """A record of every credential ever configured is a list nobody asked for."""
    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)
    credentials.remember("sk-ant-oat01-second", where, now=NOW)

    noted = json.loads(where.read_text())
    assert noted["fingerprint"] == credentials.fingerprint("sk-ant-oat01-second", noted["salt"])


def test_the_salt_is_kept_so_the_same_token_still_matches(tmp_path: Path) -> None:
    """A new salt every start would read as a new token every start, and the
    warning would never fire because the clock would never get old."""
    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)
    salt = json.loads(where.read_text())["salt"]

    credentials.remember(TOKEN, where, now=NOW + timedelta(days=1))

    assert json.loads(where.read_text())["salt"] == salt


def test_the_salt_differs_between_machines(tmp_path: Path) -> None:
    """It is not a secret, and it is what stops the derivation being a lookup."""
    one, two = tmp_path / "a.json", tmp_path / "b.json"
    credentials.remember(TOKEN, one, now=NOW)
    credentials.remember(TOKEN, two, now=NOW)

    assert json.loads(one.read_text())["salt"] != json.loads(two.read_text())["salt"]


def test_a_derivation_is_not_a_plain_digest_of_the_token(tmp_path: Path) -> None:
    """CodeQL objected to the plain digest under a rule written for password
    storage. Arguably wrong here — the input is a high-entropy token and the
    file sits beside the plaintext one — but an alert left standing teaches
    people to scroll past alerts."""
    import hashlib

    where = tmp_path / "seen.json"
    credentials.remember(TOKEN, where, now=NOW)

    noted = json.loads(where.read_text())
    assert not hashlib.sha256(TOKEN.encode()).hexdigest().startswith(noted["fingerprint"])


def test_nothing_is_said_until_it_is_nearly_a_year_old(tmp_path: Path) -> None:
    aged = credentials.remember(TOKEN, tmp_path / "seen.json", now=NOW)
    assert aged is not None

    assert not aged.worth_saying(NOW + timedelta(days=300))
    assert aged.worth_saying(NOW + timedelta(days=340))


def test_what_is_said_admits_that_it_is_a_guess(tmp_path: Path) -> None:
    """A date presented as fact would be worse than no warning: somebody would
    trust it, and it is arithmetic on an assumption."""
    aged = credentials.remember(TOKEN, tmp_path / "seen.json", now=NOW)
    assert aged is not None

    said = aged.wording(NOW + timedelta(days=340))

    assert "estimate" in said
    assert "2026-09-05" in said  # when, not a deadline
    assert "setup-token" in said  # and what to do about it


def test_a_token_already_past_its_year_says_so_plainly(tmp_path: Path) -> None:
    aged = credentials.remember(TOKEN, tmp_path / "seen.json", now=NOW)
    assert aged is not None

    said = aged.wording(NOW + timedelta(days=400))

    assert "may already have stopped working" in said


def test_no_token_is_not_this_check_s_problem(tmp_path: Path) -> None:
    """An installation on the desktop login has a different problem, and is
    told about that one elsewhere."""
    assert credentials.remember(None, tmp_path / "seen.json", now=NOW) is None
    assert credentials.remember("", tmp_path / "seen.json", now=NOW) is None


def test_a_note_that_cannot_be_read_costs_the_warning_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Nothing in the gate depends on this."""
    where = tmp_path / "seen.json"
    where.write_text("{ not json")

    aged = credentials.remember(TOKEN, where, now=NOW)

    assert aged is not None
    assert aged.first_seen == NOW


def test_a_directory_that_cannot_be_written_still_answers(tmp_path: Path) -> None:
    aged = credentials.remember(TOKEN, tmp_path / "no" / "such" / "seen.json", now=NOW)

    assert aged is not None
    assert aged.first_seen == NOW
