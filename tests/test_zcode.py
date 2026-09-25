"""Tests for the ZCode runtime: its wiring, its trust check, and a runner that
says it cannot deliver rather than pretending to.

What they rest on was measured on ZCode 3.11.2 — see
`docs/zcode-payload-notes.md`. Nothing here starts the application: the shared
setup answers "is it installed" and "does it trust this" with nothing, and a
test that wants an answer says so.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from halyard import wiring as core_wiring
from halyard.agents import registry
from halyard.agents.zcode import sessions, trust, wiring
from halyard.agents.zcode.runner import ZCodeRunner

BRIDGES = trust.BRIDGE_DIR
ZCODE = registry.discover()["zcode"]
OWN = {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]}


def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    return project


def config(project: Path) -> dict:
    return json.loads((project / wiring.CONFIG).read_text())


def with_config(project: Path, document: dict) -> None:
    (project / ".zcode").mkdir(exist_ok=True)
    (project / wiring.CONFIG).write_text(json.dumps(document))


# --- wiring -------------------------------------------------------------------


def test_wiring_writes_the_gate_the_relay_and_the_switch(tmp_path: Path) -> None:
    """All three: ZCode runs none of a file's hooks without the switch, and waits
    a minute by default where an approval can take ten."""
    project = repo(tmp_path)

    said = wiring.install(project, BRIDGES)

    hooks = config(project)["hooks"]
    assert hooks["enabled"] is True
    [gate] = hooks["events"]["PreToolUse"]
    assert gate == {
        "matcher": wiring.MATCHER,
        "hooks": [
            {
                "type": "process",
                "command": "/usr/bin/env",
                "args": ["HALYARD_RUNTIME=zcode", str(BRIDGES / "hook.sh")],
                "timeoutMs": 600_000,
            }
        ],
    }
    [relay] = hooks["events"]["Stop"]
    assert relay["hooks"][0]["args"] == ["HALYARD_RUNTIME=zcode", str(BRIDGES / "relay.py")]
    assert ("ok", "wrote PreToolUse, Stop into .zcode/config.json") in said


def test_wiring_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    """A second write would be a new declaration to ZCode, and a new declaration
    has to be trusted all over again."""
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)
    before = (project / wiring.CONFIG).read_text()

    said = wiring.install(project, BRIDGES)

    assert (project / wiring.CONFIG).read_text() == before
    assert not [text for level, text in said if level == "ok"]


def test_the_rest_of_the_workspace_configuration_is_kept(tmp_path: Path) -> None:
    """It is the workspace's whole configuration, and a team may commit it."""
    project = repo(tmp_path)
    with_config(
        project, {"mcp": {"servers": {"db": {}}}, "hooks": {"events": {"PreToolUse": [OWN]}}}
    )

    said = wiring.install(project, BRIDGES)

    written = config(project)
    assert written["mcp"] == {"servers": {"db": {}}}
    assert written["hooks"]["events"]["PreToolUse"][0] == OWN
    assert ("", "left untouched in that file: mcp") in said
    assert list((project / ".zcode").glob("config.json.*.bak"))


def test_a_file_switched_off_is_switched_back_on_and_said(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)
    document = config(project)
    document["hooks"]["enabled"] = False
    with_config(project, document)

    said = wiring.install(project, BRIDGES)

    assert config(project)["hooks"]["enabled"] is True
    assert any("was turned on" in text for _, text in said)


def test_a_file_that_cannot_be_read_is_left_alone(tmp_path: Path) -> None:
    project = repo(tmp_path)
    (project / ".zcode").mkdir()
    (project / wiring.CONFIG).write_text("{not json")

    [(level, _)] = wiring.install(project, BRIDGES)

    assert level == "fail"
    assert (project / wiring.CONFIG).read_text() == "{not json"


def test_unwiring_takes_out_only_what_was_written(tmp_path: Path) -> None:
    project = repo(tmp_path)
    with_config(project, {"hooks": {"events": {"PreToolUse": [OWN]}}})
    wiring.install(project, BRIDGES)

    said = wiring.uninstall(project, BRIDGES)

    assert config(project)["hooks"]["events"] == {"PreToolUse": [OWN]}
    assert said[0] == ("ok", "removed PreToolUse, Stop from .zcode/config.json")


def test_unwiring_the_last_hook_takes_the_switch_with_it(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)

    wiring.uninstall(project, BRIDGES)

    assert config(project) == {}


def test_unwiring_a_project_never_wired_says_nothing(tmp_path: Path) -> None:
    assert wiring.uninstall(repo(tmp_path), BRIDGES) == []


# --- trust --------------------------------------------------------------------


def status_saying(state: str) -> dict:
    """`hooks trust status --json`, in the shape 3.11.2 answers it."""
    return {
        "bundleDigest": "b" * 64,
        "reasonCode": f"workspace_hooks_{state}",
        "items": [
            {
                "event": "PreToolUse",
                "displayCommand": f"/usr/bin/env HALYARD_RUNTIME=zcode {BRIDGES / 'hook.sh'}",
                "hookDeclarationDigest": "a" * 64,
                "trustState": state,
            },
            {
                "event": "PreToolUse",
                "displayCommand": "./lint.sh",
                "hookDeclarationDigest": "c" * 64,
                "trustState": "pending_trust",
            },
        ],
    }


def test_hooks_nobody_trusted_fail_with_the_command_that_trusts_them(
    tmp_path: Path, monkeypatch
) -> None:
    """Measured: ZCode ran none of a workspace's hooks until they were trusted,
    and said so only in its own log."""
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)
    monkeypatch.setattr(trust, "status", lambda where: status_saying("pending_trust"))

    findings = trust.check_wired(project / wiring.CONFIG, project)

    assert findings[0][0] == "fail"
    grant = findings[-1][1]
    assert "hooks trust grant" in grant
    assert f"--hook-digest {'a' * 64}" in grant
    # Only Halyard's. Somebody's own hook in the same file is theirs to review.
    assert "c" * 64 not in grant


def test_hooks_zcode_trusts_are_fine(tmp_path: Path, monkeypatch) -> None:
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)
    monkeypatch.setattr(trust, "status", lambda where: status_saying("trusted_persistent"))

    assert trust.check_wired(project / wiring.CONFIG, project) == [
        ("ok", "ZCode trusts Halyard's hooks here")
    ]


def test_a_file_that_does_not_switch_hooks_on_fails(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)
    document = config(project)
    document["hooks"]["enabled"] = False
    with_config(project, document)

    assert trust.check_wired(project / wiring.CONFIG, project)[0][0] == "fail"


def test_when_zcode_cannot_be_asked_it_says_how_to_ask(tmp_path: Path) -> None:
    """The shared setup leaves nobody to ask, as on a machine without the app."""
    project = repo(tmp_path)
    wiring.install(project, BRIDGES)

    findings = trust.check_wired(project / wiring.CONFIG, project)

    assert findings[0][0] == "warn"
    assert "hooks trust status" in findings[-1][1]


def test_wire_prints_the_command_that_trusts_them(tmp_path: Path, monkeypatch, capsys) -> None:
    """`wire` used to drop the lines under a finding, which left the one that
    fixes it to `doctor` alone."""
    project = repo(tmp_path)
    monkeypatch.setattr(trust, "status", lambda where: status_saying("pending_trust"))

    core_wiring.wire(project, runtimes=(ZCODE,))

    assert "hooks trust grant" in capsys.readouterr().out


# --- the rest of the runtime --------------------------------------------------


def test_the_runner_says_it_cannot_deliver() -> None:
    """Rather than a message that looks sent and went nowhere."""
    assert asyncio.run(ZCodeRunner().send("sess_1", "hello")) is False


# --- sessions, by the titles ZCode keeps in its own database -------------------

#: Milliseconds, as ZCode keeps them.
EARLIER = 1_757_950_000_000
LATER = EARLIER + 60_000


def zcode_sessions(home: Path, *rows: tuple) -> None:
    """ZCode's own database, with the columns `sessions` reads."""
    database = home / sessions.DATABASE
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, "
            "directory TEXT NOT NULL, title TEXT NOT NULL, title_source TEXT NOT NULL, "
            "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
            "time_archived INTEGER)"
        )
        db.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        db.commit()


def session(sid: str, title: str, source: str = "custom", **more) -> tuple:
    """One row: a top-level session in /repo, updated at `LATER` unless told."""
    return (
        sid,
        more.get("parent"),
        "/repo",
        title,
        source,
        EARLIER,
        more.get("updated", LATER),
        more.get("archived"),
    )


def test_the_runtime_finds_its_sessions_in_zcodes_database() -> None:
    assert ZCODE.find_session is sessions.find_session
    assert ZCODE.list_sessions is sessions.list_sessions


def test_a_session_is_found_by_the_title_somebody_gave_it(tmp_path: Path) -> None:
    zcode_sessions(tmp_path, session("sess_1", "alpha-engine-zdriver"))

    found = sessions.find_session("Alpha-Engine-ZDriver", home=tmp_path)

    assert found is not None
    assert (found.session_id, found.cwd, found.named_by_a_person) == ("sess_1", "/repo", True)


def test_a_generated_title_is_found_and_said_to_be_generated(tmp_path: Path) -> None:
    """`doctor` warns about it, as for Claude Code: a generated title moves."""
    zcode_sessions(tmp_path, session("sess_1", "zcode hooks and opencode", "generated"))

    found = sessions.find_session("zcode hooks and opencode", home=tmp_path)

    assert found is not None
    assert found.named_by_a_person is False


def test_a_first_prompt_is_not_a_name(tmp_path: Path) -> None:
    zcode_sessions(tmp_path, session("sess_1", "delete the old reports", "first_input"))

    assert sessions.find_session("delete the old reports", home=tmp_path) is None
    assert sessions.list_sessions(home=tmp_path) == []


def test_subagents_and_archived_sessions_are_not_seats(tmp_path: Path) -> None:
    zcode_sessions(
        tmp_path,
        session("sess_1", "alpha-engine-zdriver"),
        session("sess_2", "Explore the loader", "generated", parent="sess_1"),
        session("sess_3", "old-driver", archived=LATER),
    )

    assert [ref.name for ref in sessions.list_sessions(home=tmp_path)] == ["alpha-engine-zdriver"]
    assert sessions.find_session("Explore the loader", home=tmp_path) is None
    assert sessions.find_session("old-driver", home=tmp_path) is None


def test_a_title_set_by_hand_wins_over_a_newer_generated_one(tmp_path: Path) -> None:
    zcode_sessions(
        tmp_path,
        session("sess_1", "driver", updated=EARLIER),
        session("sess_2", "driver", "generated"),
    )

    found = sessions.find_session("driver", home=tmp_path)

    assert found is not None
    assert found.session_id == "sess_1"


def test_sessions_are_listed_newest_first_and_found_by_id_too(tmp_path: Path) -> None:
    zcode_sessions(
        tmp_path,
        session("sess_1", "older", updated=EARLIER),
        session("sess_2", "newer"),
    )

    listed = sessions.list_sessions(home=tmp_path)

    assert [ref.name for ref in listed] == ["newer", "older"]
    assert listed[0].last_active is not None and listed[1].last_active is not None
    assert listed[0].last_active > listed[1].last_active
    found = sessions.find_session("sess_1", home=tmp_path)
    assert found is not None and found.name == "older"


def test_without_zcodes_database_there_is_nothing_to_find(tmp_path: Path) -> None:
    assert sessions.find_session("alpha-engine-zdriver", home=tmp_path) is None
    assert sessions.list_sessions(home=tmp_path) == []


def test_a_file_that_is_not_a_database_is_nothing_rather_than_an_error(tmp_path: Path) -> None:
    database = tmp_path / sessions.DATABASE
    database.parent.mkdir(parents=True)
    database.write_text("not a database")

    assert sessions.find_session("alpha-engine-zdriver", home=tmp_path) is None


def test_without_the_application_it_says_where_it_looked() -> None:
    [(level, text)] = ZCODE.check_available()

    assert level == "fail"
    assert "ZCode.app" in text
