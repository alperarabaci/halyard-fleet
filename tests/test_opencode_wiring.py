"""Tests for putting the opencode gate on a project.

Two properties carry this file. The first is that wiring writes *both* halves:
a plugin nobody consults is the failure this runtime is most able to produce,
because opencode asks about nothing unless its own configuration says to, and
a project in that state looks wired from every angle.

The second is that the project's own configuration survives. That file is not
Halyard's — in the case this was written against it carries an `instructions`
list pointing at a team's standards — and a wiring step that quietly replaced
it would cost more than the gate is worth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from halyard.agents import opencode
from halyard.agents.opencode import wiring


@pytest.fixture
def bridges(tmp_path: Path) -> Path:
    """A stand-in for this install's `bridge/` directory."""
    where = tmp_path / "bridge"
    where.mkdir()
    (where / wiring.SOURCE).write_text("export const HalyardGate = async () => ({})\n")
    return where


@pytest.fixture
def project(tmp_path: Path) -> Path:
    where = tmp_path / "project"
    where.mkdir()
    return where


def levels(said: list[tuple[str, str]]) -> set[str]:
    return {level for level, _ in said if level}


def config_of(project: Path) -> dict:
    return json.loads((project / wiring.CONFIG).read_text(encoding="utf-8"))


# --- writing both halves -----------------------------------------------------


def test_the_plugin_lands_where_the_runtime_looks_for_it(project: Path, bridges: Path) -> None:
    """Measured: this directory is loaded at startup with no config entry."""
    wiring.install(project, bridges)

    wanted = project / ".opencode" / "plugins" / "halyard.ts"
    assert wanted.is_file()
    assert wanted.read_text() == (bridges / wiring.SOURCE).read_text()


def test_the_runtime_is_also_told_to_ask(project: Path, bridges: Path) -> None:
    """The half that is easy to forget, and the one whose absence is silent:
    without it the plugin loads and is never consulted about anything."""
    wiring.install(project, bridges)

    assert config_of(project)["permission"] == wiring.ASK


def test_a_plugin_without_the_asking_is_reported_as_no_gate(project: Path, bridges: Path) -> None:
    """Which is the whole reason `check_wired` looks at two files."""
    wiring.install(project, bridges)
    (project / wiring.CONFIG).write_text(json.dumps({"permission": {"bash": "allow"}}))

    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "fail" in levels(said)
    assert any("does not ask" in text for _, text in said)


def test_a_project_wired_both_ways_passes(project: Path, bridges: Path) -> None:
    wiring.install(project, bridges)

    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "fail" not in levels(said)


def test_a_missing_plugin_says_what_to_run(project: Path) -> None:
    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "fail" in levels(said)
    assert any("halyard wire" in text for _, text in said)


# --- somebody else's file ----------------------------------------------------


def test_everything_already_in_that_file_survives(project: Path, bridges: Path) -> None:
    """The failure this is guarding against is losing a team's standards list
    to a wiring step that thought the file was its own."""
    theirs = {
        "$schema": "https://opencode.ai/config.json",
        "instructions": ["NOTES/project-invariants/INVARIANTS.md"],
    }
    (project / wiring.CONFIG).write_text(json.dumps(theirs))

    wiring.install(project, bridges)

    after = config_of(project)
    assert after["instructions"] == theirs["instructions"]
    assert after["$schema"] == theirs["$schema"]
    assert after["permission"] == wiring.ASK


def test_what_was_left_alone_is_said_out_loud(project: Path, bridges: Path) -> None:
    """ "Nothing was reported" is not the same reassurance as "it is still
    there", and this file is somebody's work."""
    (project / wiring.CONFIG).write_text(json.dumps({"instructions": ["NOTES/x.md"]}))

    said = wiring.install(project, bridges)

    assert any("left untouched" in text and "instructions" in text for _, text in said)


def test_a_narrower_permission_already_there_is_widened_not_replaced(
    project: Path, bridges: Path
) -> None:
    """A project asking about something Halyard does not list keeps asking."""
    (project / wiring.CONFIG).write_text(json.dumps({"permission": {"doom_loop": "ask"}}))

    wiring.install(project, bridges)

    assert config_of(project)["permission"]["doom_loop"] == "ask"
    assert config_of(project)["permission"]["bash"] == "ask"


# --- doing it twice ----------------------------------------------------------


def test_wiring_twice_changes_nothing_and_says_so(project: Path, bridges: Path) -> None:
    wiring.install(project, bridges)

    said = wiring.install(project, bridges)

    assert "ok" not in levels(said), "nothing was written, so nothing should claim it was"
    assert any("already" in text for _, text in said)


def test_a_stale_plugin_is_replaced(project: Path, bridges: Path) -> None:
    """The bridge travels with this checkout, so a project wired by an older
    Halyard has an older gate in it."""
    wanted = project / wiring.PLUGINS / wiring.PLUGIN
    wanted.parent.mkdir(parents=True)
    wanted.write_text("// what an earlier install wrote\n")

    wiring.install(project, bridges)

    assert wanted.read_text() == (bridges / wiring.SOURCE).read_text()


# --- taking it off -----------------------------------------------------------


def test_unwiring_removes_the_plugin(project: Path, bridges: Path) -> None:
    wiring.install(project, bridges)

    said = wiring.uninstall(project, bridges)

    assert not (project / wiring.PLUGINS / wiring.PLUGIN).exists()
    assert "ok" in levels(said)


def test_unwiring_leaves_the_asking_alone(project: Path, bridges: Path) -> None:
    """Switching it back off would be Halyard deciding a project should stop
    asking, which is not what taking the gate off means. The questions go back
    to the desk, which is where they were before any of this."""
    wiring.install(project, bridges)

    wiring.uninstall(project, bridges)

    assert config_of(project)["permission"] == wiring.ASK


def test_unwiring_an_unwired_project_says_nothing(project: Path, bridges: Path) -> None:
    """So `unwire` can say "nothing was wired here" once, rather than four
    runtimes each saying it."""
    assert wiring.uninstall(project, bridges) == []


# --- when things are broken --------------------------------------------------


def test_a_missing_bridge_is_a_failure_not_a_traceback(project: Path, tmp_path: Path) -> None:
    said = wiring.install(project, tmp_path / "no-bridges-here")

    assert "fail" in levels(said)


def test_a_config_that_cannot_be_parsed_stops_before_overwriting_it(
    project: Path, bridges: Path
) -> None:
    """The plugin still goes in — that half is safe and useful on its own — but
    a file that could not be read is never written over. What is in there might
    be a comment, a merge conflict, or a morning's work."""
    (project / wiring.CONFIG).write_text("{ not json at all")

    said = wiring.install(project, bridges)

    assert "fail" in levels(said)
    assert (project / wiring.CONFIG).read_text() == "{ not json at all"
    assert (project / wiring.PLUGINS / wiring.PLUGIN).is_file()
