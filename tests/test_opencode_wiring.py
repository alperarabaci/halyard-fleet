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


# --- what "always allow" writes ----------------------------------------------
#
# Pressing it at the keyboard makes opencode rewrite the category into an object
# of patterns. Measured on a real project: `{"*": "ask", "grep *": "allow", …}`,
# which asks about everything except four read-only commands. Reading only the
# string called that project ungated and printed `halyard wire` underneath —
# and wiring would then have replaced the object with `"ask"`, deleting the four
# exceptions to fix a gate that was working.


def asking_except(*allowed: str) -> dict:
    return {"permission": {**wiring.ASK, "bash": {"*": "ask", **{p: "allow" for p in allowed}}}}


def test_a_category_that_asks_by_pattern_is_gated(project: Path, bridges: Path) -> None:
    wiring.install(project, bridges)
    (project / wiring.CONFIG).write_text(json.dumps(asking_except("grep *", "ls *")))

    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "fail" not in levels(said)


def test_the_patterns_it_answers_alone_are_named(project: Path, bridges: Path) -> None:
    """A warning, because it is true and worth knowing, and not a failure,
    because somebody chose it and the gate still sees everything else."""
    wiring.install(project, bridges)
    (project / wiring.CONFIG).write_text(json.dumps(asking_except("grep *", "ls *")))

    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "warn" in levels(said)
    assert any("grep *" in text and "ls *" in text for _, text in said)
    assert any("audit" in text for _, text in said)


def test_a_pattern_object_that_allows_everything_is_still_ungated(
    project: Path, bridges: Path
) -> None:
    """The catch-all is the whole question. Without one that asks, the object
    is a list of exceptions with nothing behind it."""
    wiring.install(project, bridges)
    (project / wiring.CONFIG).write_text(
        json.dumps({"permission": {**wiring.ASK, "bash": {"*": "allow", "rm *": "ask"}}})
    )

    said = opencode.check_wired(
        hooks_file=project / wiring.PLUGINS / wiring.PLUGIN, project_dir=project
    )

    assert "fail" in levels(said)
    assert any("does not ask about bash" in text for _, text in said)


def test_wiring_does_not_delete_what_always_allow_wrote(project: Path, bridges: Path) -> None:
    """The destructive half. The FAIL above printed `halyard wire` as the
    remedy, and running it would have thrown these away."""
    (project / wiring.CONFIG).write_text(json.dumps(asking_except("grep *", "cat *")))

    wiring.install(project, bridges)

    assert config_of(project)["permission"]["bash"] == {
        "*": "ask",
        "grep *": "allow",
        "cat *": "allow",
    }


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


# --- the port, without which the gate can ask and never answer ----------------


def test_a_running_opencode_is_reported_as_reachable(monkeypatch) -> None:
    from halyard.agents import opencode

    monkeypatch.setattr(opencode, "reachable", lambda port: (True, "answered 200"))
    monkeypatch.setattr(opencode, "_available_binary", lambda: [("ok", "opencode at /x")])
    monkeypatch.setattr(
        "halyard.core.config_file.runtime_settings",
        lambda *a, **k: {"opencode": _settings(port=4096)},
    )

    said = opencode.check_available()

    assert any(level == "ok" and "answering on port 4096" in text for level, text in said)


def test_an_opencode_started_without_a_port_is_called_out(monkeypatch) -> None:
    """The failure this check exists for. Measured: `opencode` alone listens on
    no TCP port, so the plugin delivers the question and cannot answer it —
    while every file on disk says the project is gated.
    """
    from halyard.agents import opencode

    monkeypatch.setattr(opencode, "reachable", lambda port: (False, "connection refused"))
    monkeypatch.setattr(opencode, "_available_binary", lambda: [("ok", "opencode at /x")])
    monkeypatch.setattr(
        "halyard.core.config_file.runtime_settings",
        lambda *a, **k: {"opencode": _settings(port=4096)},
    )

    said = opencode.check_available()

    # A failure rather than a warning: everything after this asks opencode
    # which session a seat means, and with nobody answering each of those comes
    # back empty and is reported as a session that does not exist.
    assert any(level == "fail" for level, _ in said)
    assert any("--port 4096" in text for _, text in said)


def test_no_configured_port_says_what_to_add(monkeypatch) -> None:
    from halyard.agents import opencode

    monkeypatch.setattr(opencode, "_available_binary", lambda: [("ok", "opencode at /x")])
    monkeypatch.setattr("halyard.core.config_file.runtime_settings", lambda *a, **k: {})

    said = opencode.check_available()

    assert any("runtimes: opencode: port:" in text for _, text in said)


def test_a_missing_cli_stops_before_asking_about_ports(monkeypatch) -> None:
    """Nothing to reach, and a second complaint about a port would bury the
    one that matters."""
    from halyard.agents import opencode

    monkeypatch.setattr(opencode, "_binary", lambda: None)

    said = opencode.check_available()

    assert said[0][0] == "fail"
    assert not any("port" in text for _, text in said)


def _settings(**rest):
    from halyard.core.config_file import RuntimeSettings

    return RuntimeSettings(name="opencode", **rest)


# --- finding the session a seat names -----------------------------------------


def _listed(*sessions):
    return list(sessions)


def _session(id_: str, title: str, updated: int = 1_000_000):
    return {
        "id": id_,
        "title": title,
        "directory": "/a/project",
        "time": {"created": 500_000, "updated": updated},
    }


def test_a_seat_finds_the_session_it_names(monkeypatch) -> None:
    """Sessions here are addressed by their title, and a long-lived one is
    titled to match the seat. Measured on a real project, where the chosen name
    and the generated ones are plain to tell apart by eye."""
    from halyard.agents import opencode

    monkeypatch.setattr(
        opencode,
        "_sessions",
        lambda directory=None: _listed(
            _session("ses_1", "Kalan kapatma promptu — capstone-ekran"),
            _session("ses_2", "alpha-engine-opencode-driver"),
        ),
    )

    found = opencode.find_session("alpha-engine-opencode-driver")

    assert found is not None
    assert found.session_id == "ses_2"
    assert found.cwd == "/a/project"
    assert found.named_by_a_person, "the configuration and the runtime agree on this name"


def test_an_id_is_accepted_as_well_as_a_title(monkeypatch) -> None:
    """An id is unreadable and permanent, and somebody holding one should not
    be told to go and find a title for it first."""
    from halyard.agents import opencode

    monkeypatch.setattr(
        opencode, "_sessions", lambda directory=None: _listed(_session("ses_1", "some title"))
    )

    found = opencode.find_session("ses_1")

    assert found is not None
    assert not found.named_by_a_person, "an id says nothing about who chose the title"


def test_an_unreachable_opencode_finds_nothing_rather_than_raising(monkeypatch) -> None:
    """`_sessions` answers None when nobody answered, and every caller here is
    somewhere a person is waiting."""
    from halyard.agents import opencode

    monkeypatch.setattr(opencode, "_sessions", lambda directory=None: None)

    assert opencode.find_session("anything") is None
    assert opencode.list_sessions() == []


def test_the_title_is_matched_the_way_people_type_it(monkeypatch) -> None:
    from halyard.agents import opencode

    monkeypatch.setattr(
        opencode, "_sessions", lambda directory=None: _listed(_session("ses_1", "Alpha-Driver"))
    )

    assert opencode.find_session("  alpha-driver ") is not None


def test_sessions_are_listed_newest_first(monkeypatch) -> None:
    """What `halyard init` offers, and the order it offers them in."""
    from halyard.agents import opencode

    monkeypatch.setattr(
        opencode,
        "_sessions",
        lambda directory=None: _listed(
            _session("ses_old", "older", updated=1),
            _session("ses_new", "newer", updated=9_999_999),
        ),
    )

    assert [ref.session_id for ref in opencode.list_sessions()] == ["ses_new", "ses_old"]
