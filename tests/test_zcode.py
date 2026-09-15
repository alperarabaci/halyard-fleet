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
from pathlib import Path

from halyard import wiring as core_wiring
from halyard.agents import registry
from halyard.agents.zcode import trust, wiring
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


def test_a_zcode_seat_has_no_session_to_find() -> None:
    """ZCode names no sessions; its seat is found by the project instead."""
    assert ZCODE.find_session("alpha-engine-driver") is None
    assert ZCODE.list_sessions() == []


def test_without_the_application_it_says_where_it_looked() -> None:
    [(level, text)] = ZCODE.check_available()

    assert level == "fail"
    assert "ZCode.app" in text
