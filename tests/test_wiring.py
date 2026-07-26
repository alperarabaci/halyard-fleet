"""Adding and removing the gate without taking anything else with it.

The file being edited belongs to Claude Code as much as to Halyard. Every test
here is about that: what must survive a write, and what must not be removed by
somebody else's uninstall.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from halyard import wiring
from halyard.agents import registry
from halyard.agents.codex import trust

CLAUDE = registry.get("claude-code")


def repo(tmp_path: Path, settings: dict | None = None) -> Path:
    """A project that looks like a checkout, optionally already configured."""
    (tmp_path / ".git").mkdir()
    if settings is not None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.local.json").write_text(json.dumps(settings, indent=2))
    return tmp_path


def read(path: Path) -> dict:
    return json.loads((path / ".claude" / "settings.local.json").read_text())


def test_wiring_keeps_the_permission_list(tmp_path: Path) -> None:
    """The failure this module exists to prevent.

    Losing `permissions.allow` produces no error and no obvious symptom — the
    session simply starts asking again about commands it settled months ago,
    and nobody connects that to a config edit from days earlier.
    """
    project = repo(tmp_path, {"permissions": {"allow": ["Bash(uv run *)", "WebSearch"]}})

    wiring.wire(project, runtimes=(CLAUDE,))

    assert read(project)["permissions"]["allow"] == ["Bash(uv run *)", "WebSearch"]
    assert read(project)["hooks"]["PreToolUse"]


def test_wiring_keeps_a_backup(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["WebSearch"]}})

    wiring.wire(project, runtimes=(CLAUDE,))

    backups = list((project / ".claude").glob("settings.local.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["permissions"]["allow"] == ["WebSearch"]


def test_backup_exists_before_settings_are_written(tmp_path: Path, monkeypatch) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["WebSearch"]}})
    settings = project / ".claude" / "settings.local.json"
    original = settings.read_bytes()
    write = wiring._write

    def observe_write(path: Path, config: dict) -> None:
        backups = list(path.parent.glob(f"{path.name}.*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == original
        write(path, config)

    monkeypatch.setattr(wiring, "_write", observe_write)

    wiring.wire(project, runtimes=(CLAUDE,))


def test_wiring_preserves_the_complete_claude_document(tmp_path: Path) -> None:
    original = {
        "permissions": {
            "allow": ["Bash(uv run *)", "WebSearch"],
            "deny": ["Read(./secrets/**)"],
            "ask": ["Bash(git push *)"],
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "/other/write-check.sh"}],
                }
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/other/session-start.sh"}]}
            ],
        },
        "enabledPlugins": {"example@marketplace": True},
        "env": {"EXAMPLE_MODE": "careful"},
    }
    project = repo(tmp_path, original)
    settings = project / ".claude" / "settings.local.json"
    before = settings.read_bytes()

    wiring.wire(project, runtimes=(CLAUDE,))

    written = read(project)
    assert written["permissions"] == original["permissions"]
    assert written["enabledPlugins"] == original["enabledPlugins"]
    assert written["env"] == original["env"]
    assert written["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    assert original["hooks"]["PreToolUse"][0] in written["hooks"]["PreToolUse"]
    backup = next(settings.parent.glob(f"{settings.name}.*.bak"))
    assert backup.read_bytes() == before


def test_wiring_an_untouched_project_creates_the_file(tmp_path: Path) -> None:
    project = repo(tmp_path)

    assert wiring.wire(project, runtimes=(CLAUDE,)) == 0

    events = read(project)["hooks"]
    assert "PreToolUse" in events
    assert "Stop" in events


def test_wiring_twice_does_not_duplicate_the_hook(tmp_path: Path) -> None:
    """A hook listed twice would ask twice for one command."""
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CLAUDE,))
    wiring.wire(project, runtimes=(CLAUDE,))

    groups = read(project)["hooks"]["PreToolUse"]
    ours = [g for g in groups if any(wiring._is_ours(h["command"]) for h in g["hooks"])]
    assert len(ours) == 1


def test_unwiring_leaves_somebody_elses_hook_alone(tmp_path: Path) -> None:
    """Removal is by path, so this cannot uninstall a tool it did not install."""
    project = repo(
        tmp_path,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/somebody/elses/hook.sh"}],
                    }
                ]
            }
        },
    )
    wiring.wire(project, runtimes=(CLAUDE,))

    wiring.unwire(project)

    remaining = read(project)["hooks"]["PreToolUse"]
    assert len(remaining) == 1
    assert remaining[0]["hooks"][0]["command"] == "/somebody/elses/hook.sh"


def test_unwiring_keeps_the_permission_list(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["Bash(uv run *)"]}})
    wiring.wire(project, runtimes=(CLAUDE,))

    wiring.unwire(project)

    written = read(project)
    assert written["permissions"]["allow"] == ["Bash(uv run *)"]
    assert "hooks" not in written


def test_unwiring_what_was_never_wired_changes_nothing(tmp_path: Path) -> None:
    project = repo(tmp_path, {"permissions": {"allow": ["WebSearch"]}})

    assert wiring.unwire(project) == 0

    assert read(project) == {"permissions": {"allow": ["WebSearch"]}}
    assert not list((project / ".claude").glob("*.bak"))


def test_a_subdirectory_is_wired_at_the_repository_root(tmp_path: Path) -> None:
    """Where Claude Code actually looks — measured, not assumed.

    A session opened under a monorepo's web app is gated by the `.claude/` at
    the top of the repository. Writing a second one next to the session would
    gate nothing while looking like it had.
    """
    project = repo(tmp_path)
    inside = project / "web" / "src"
    inside.mkdir(parents=True)

    wiring.wire(inside, runtimes=(CLAUDE,))

    assert (project / ".claude" / "settings.local.json").exists()
    assert not (inside / ".claude").exists()


def test_a_directory_outside_a_repository_is_wired_where_it_stands(tmp_path: Path) -> None:
    """With no `.git` above it, a parent's hooks never fire — so do not go up."""
    loose = tmp_path / "not-a-repo"
    loose.mkdir()

    wiring.wire(loose, runtimes=(CLAUDE,))

    assert (loose / ".claude" / "settings.local.json").exists()


def test_a_broken_settings_file_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    """Overwriting unreadable JSON would destroy whatever it was meant to hold."""
    project = repo(tmp_path)
    broken = project / ".claude" / "settings.local.json"
    broken.parent.mkdir(exist_ok=True)
    broken.write_text("{ this is not json")

    try:
        wiring.wire(project, runtimes=(CLAUDE,))
    except SystemExit as stop:
        assert "not valid JSON" in str(stop)
    else:
        raise AssertionError("wiring should refuse a file it cannot parse")

    assert broken.read_text() == "{ this is not json"


# --- a second runtime -------------------------------------------------------


CODEX = registry.get("codex")


def test_codex_hooks_go_in_their_own_file(tmp_path: Path) -> None:
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    assert written["hooks"]["PreToolUse"]
    assert written["hooks"]["PermissionRequest"]
    assert not (project / ".claude").exists()


def test_wiring_preserves_and_backs_up_the_complete_codex_document(tmp_path: Path) -> None:
    project = repo(tmp_path)
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    hooks_file = codex_dir / "hooks.json"
    original = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/other/codex-session-start.sh"}]}
            ],
            "PreToolUse": [
                {
                    "matcher": "^apply_patch$",
                    "hooks": [{"type": "command", "command": "/other/patch-check.sh"}],
                }
            ],
        },
        "futureCodexField": {"must": ["survive", "unchanged"]},
    }
    hooks_file.write_text(json.dumps(original, indent=4) + "\n", encoding="utf-8")
    before = hooks_file.read_bytes()

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads(hooks_file.read_text())
    assert written["futureCodexField"] == original["futureCodexField"]
    assert written["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    assert original["hooks"]["PreToolUse"][0] in written["hooks"]["PreToolUse"]
    assert written["hooks"]["PermissionRequest"]
    backup = next(codex_dir.glob(f"{hooks_file.name}.*.bak"))
    assert backup.read_bytes() == before


def test_permission_request_is_codex_only(tmp_path: Path) -> None:
    project = repo(tmp_path)

    wiring.wire(project, runtimes=tuple(registry.discover().values()))

    claude = json.loads((project / ".claude" / "settings.local.json").read_text())
    codex = json.loads((project / ".codex" / "hooks.json").read_text())
    assert "PermissionRequest" not in claude["hooks"]
    assert codex["hooks"]["PermissionRequest"]


def test_codex_matches_the_app_as_well_as_the_cli(tmp_path: Path) -> None:
    """`codex exec` calls the shell tool `Bash`. The desktop app does not.

    It calls a tool named `exec` whose input is JavaScript, with the shell call
    inside it. A matcher of `^Bash$` gates the CLI and ignores the app — a gate
    that is installed, reports itself installed, and never fires for the way
    the work is actually done.
    """
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    matcher = written["hooks"]["PreToolUse"][0]["matcher"]
    assert "Bash" in matcher
    assert "exec" in matcher


def test_a_matcher_from_an_older_release_is_corrected(tmp_path: Path) -> None:
    """Leaving a stale one is the quiet kind of wrong: the file is there,
    doctor is happy, and half the tool calls are not gated."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    written = json.loads(hooks_file.read_text())
    written["hooks"]["PreToolUse"][0]["matcher"] = "^Bash$"
    hooks_file.write_text(json.dumps(written))

    wiring.wire(project, runtimes=(CODEX,))

    corrected = json.loads(hooks_file.read_text())
    assert "exec" in corrected["hooks"]["PreToolUse"][0]["matcher"]


def test_the_command_is_absolute_because_codex_expands_nothing(tmp_path: Path) -> None:
    """Codex has no project-directory variable — only $CODEX_HOME.

    A file carrying `$CLAUDE_PROJECT_DIR` does not fail to load under Codex.
    The hook runs and dies looking for a directory by that literal name, which
    is what this repository's own `hook: Stop Failed` turned out to be.
    """
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((project / ".codex" / "hooks.json").read_text())
    command = written["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith("/")
    assert "$" not in command


def test_unwiring_covers_a_runtime_whose_cli_is_gone(tmp_path: Path) -> None:
    """Removal is attempted everywhere, not only where a CLI is installed.

    A hook left behind after a CLI is uninstalled still points at a bridge, and
    the next person to install that CLI inherits a gate they never asked for.
    """
    project = repo(tmp_path)
    wiring.wire(project, runtimes=tuple(registry.discover().values()))

    wiring.unwire(project)

    assert json.loads((project / ".codex" / "hooks.json").read_text()) == {}
    assert json.loads((project / ".claude" / "settings.local.json").read_text()) == {}


def test_a_hook_with_no_trust_record_is_reported(tmp_path: Path) -> None:
    """Codex skips an untrusted hook in silence — measured.

    The turn completes, nothing is printed, and a PreToolUse gate that is not
    trusted is not a gate. Absence of a record is the one thing that can be
    stated for certain, so it is the thing reported.
    """
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    empty = tmp_path / "config.toml"
    empty.write_text("")

    pending = trust.untrusted(hooks_file, empty)

    assert len(pending) == 3
    assert all(str(hooks_file) in key for key in pending)
    # Codex writes the event in snake case; matching `pretooluse` finds
    # nothing and reports a trusted hook as never trusted.
    assert any(key.endswith(":pre_tool_use:0:0") for key in pending)


def test_a_recorded_hook_is_not_reported_as_untrusted(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    config = tmp_path / "config.toml"
    config.write_text(
        "".join(
            f'[hooks.state."{key}"]\ntrusted_hash = "sha256:x"\n'
            for key in trust.trust_keys(hooks_file)
        )
    )

    assert trust.untrusted(hooks_file, config) == []


def test_editing_the_hooks_file_makes_trust_stale(tmp_path: Path) -> None:
    """The dangerous reading is the other one.

    A trust key that still exists with an outdated hash looks exactly like a
    trusted hook. Codex disagrees and says nothing about it.
    """
    project = repo(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text("")
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    os.utime(hooks_file, (time.time() + 10, time.time() + 10))

    assert trust.is_stale(hooks_file, config) is True


def test_trust_is_not_claimed_to_be_fresh(tmp_path: Path) -> None:
    """The inference only runs one way: a newer config proves nothing, because
    Codex rewrites it for unrelated reasons."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CODEX,))
    hooks_file = project / ".codex" / "hooks.json"
    config = tmp_path / "config.toml"
    config.write_text("")
    os.utime(config, (time.time() + 10, time.time() + 10))

    assert trust.is_stale(hooks_file, config) is False


# --- Antigravity, whose file is a third shape ---------------------------------

ANTIGRAVITY = registry.get("antigravity")


def antigravity_hooks(project: Path) -> dict:
    return json.loads((project / ".agents" / "hooks.json").read_text())


def test_antigravity_hooks_are_keyed_by_name_not_wrapped(tmp_path: Path) -> None:
    """A third spelling of the same idea, and it is not `{"hooks": {...}}`.

    Every top-level key is a hook *name*; all the names contributing to an
    event are merged and run in turn. Writing the other two runtimes' wrapper
    here would produce a hook called "hooks" and no gate.
    """
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(ANTIGRAVITY,))

    written = antigravity_hooks(project)
    assert list(written) == ["halyard"]
    assert "hooks" not in written
    assert set(written["halyard"]) == {"PreToolUse", "Stop", "PreInvocation"}


def test_pretooluse_is_grouped_and_stop_is_flat(tmp_path: Path) -> None:
    """The difference that would otherwise cost the relay silently.

    Antigravity groups the tool events behind a `matcher` and takes every other
    event as a flat list of handlers. A `Stop` written in the grouped shape has
    no `command` where Antigravity looks for one, so no reply ever reaches a
    phone — with the file present and everything reporting wired.
    """
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(ANTIGRAVITY,))
    spec = antigravity_hooks(project)["halyard"]

    assert spec["PreToolUse"][0]["matcher"] == "run_command"
    assert spec["PreToolUse"][0]["hooks"][0]["command"].endswith("hook.sh")
    assert "hooks" not in spec["Stop"][0], "Stop takes handlers directly"
    assert spec["Stop"][0]["command"].endswith("relay.py")


def test_another_tools_hooks_in_that_file_survive(tmp_path: Path) -> None:
    """The name-keyed shape exists so two tools can gate one project. Wiring
    must behave like the second of the two, not the only one."""
    project = repo(tmp_path)
    (project / ".agents").mkdir()
    (project / ".agents" / "hooks.json").write_text(
        json.dumps(
            {
                "lint-checker": {
                    "PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]
                }
            }
        )
    )

    wiring.wire(project, runtimes=(ANTIGRAVITY,))

    written = antigravity_hooks(project)
    assert written["lint-checker"]["PostToolUse"][0]["hooks"][0]["command"] == "./lint.sh"
    assert "halyard" in written


def test_a_disabled_gate_is_turned_back_on(tmp_path: Path) -> None:
    """`"enabled": false` leaves a file that looks exactly like a wired one and
    runs none of it. Wiring means the gate works."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(ANTIGRAVITY,))
    path = project / ".agents" / "hooks.json"
    config = json.loads(path.read_text())
    config["halyard"]["enabled"] = False
    path.write_text(json.dumps(config))

    wiring.wire(project, runtimes=(ANTIGRAVITY,))

    assert antigravity_hooks(project)["halyard"]["enabled"] is True


def test_wiring_antigravity_twice_does_not_duplicate_the_hook(tmp_path: Path) -> None:
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(ANTIGRAVITY,))
    wiring.wire(project, runtimes=(ANTIGRAVITY,))

    spec = antigravity_hooks(project)["halyard"]
    assert len(spec["PreToolUse"]) == 1
    assert len(spec["Stop"]) == 1


def test_unwiring_antigravity_removes_the_whole_name(tmp_path: Path) -> None:
    """A name left holding nothing but `enabled` is a husk somebody has to work
    out the meaning of."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(ANTIGRAVITY,))

    wiring.unwire(project, runtimes=(ANTIGRAVITY,))

    assert "halyard" not in antigravity_hooks(project)


def test_unwiring_antigravity_leaves_somebody_elses_hook_alone(tmp_path: Path) -> None:
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(ANTIGRAVITY,))
    path = project / ".agents" / "hooks.json"
    config = json.loads(path.read_text())
    config["safety-gate"] = {
        "PreToolUse": [{"matcher": "run_command", "hooks": [{"command": "/other/check.sh"}]}]
    }
    path.write_text(json.dumps(config))

    wiring.unwire(project, runtimes=(ANTIGRAVITY,))

    written = antigravity_hooks(project)
    assert written["safety-gate"]["PreToolUse"][0]["hooks"][0]["command"] == "/other/check.sh"
    assert "halyard" not in written


# --- which runtimes a project actually gets ----------------------------------


def configured_project(tmp_path: Path, runtimes: list[str]) -> Path:
    """A checkout with a `halyard.yaml` describing seats for those runtimes."""
    root = tmp_path / "alpha-engine"
    root.mkdir()
    project = repo(root)
    seats = "\n".join(
        f"      s{index}:\n"
        f"        runtime: {runtime}\n"
        f"        session: session-{index}\n"
        f'        chat: "-100{index}"\n'
        for index, runtime in enumerate(runtimes)
    )
    (tmp_path / "halyard.yaml").write_text(
        f"projects:\n  alpha-engine:\n    path: {project}\n    seats:\n" + (seats or "      {}\n")
    )
    return project


def test_only_the_runtimes_the_project_is_configured_for_are_wired(
    tmp_path: Path, monkeypatch
) -> None:
    """The bug this is a regression for, and it was wrong in both directions.

    A Mac mini whose `halyard.yaml` described two Claude Code seats got
    Antigravity's hooks written into that project — because Antigravity was
    installed — and no Claude Code hooks at all, because its binary lives
    inside an app bundle rather than on PATH. The file said exactly which
    runtimes that project uses and nothing read it.
    """
    project = configured_project(tmp_path, ["claude-code"])
    monkeypatch.chdir(tmp_path)

    wiring.wire(project)

    assert (project / ".claude" / "settings.local.json").exists()
    assert not (project / ".agents").exists(), "a runtime nobody configured must not be wired"


def test_an_installed_runtime_nobody_configured_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    """Being on the machine is not a reason to gate a project with it."""
    project = configured_project(tmp_path, ["codex"])
    monkeypatch.chdir(tmp_path)

    wiring.wire(project)

    assert (project / ".codex" / "hooks.json").exists()
    assert not (project / ".claude").exists()
    assert not (project / ".agents").exists()


def test_a_configured_runtime_is_wired_even_when_its_cli_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """The hooks file is shared; the machine that runs that runtime may not be
    this one. Silence would be the wrong answer either way — the note says so
    while the gate still gets written."""
    project = configured_project(tmp_path, ["antigravity"])
    monkeypatch.chdir(tmp_path)
    # Patched at the seam rather than on the spec, which is frozen on purpose:
    # a descriptor somebody can reach in and edit is a descriptor that stops
    # describing the package it came from.
    monkeypatch.setattr("halyard.agents.antigravity.find_antigravity_binary", lambda *a: None)
    monkeypatch.setattr("shutil.which", lambda name: None)

    wiring.wire(project)

    assert (project / ".agents" / "hooks.json").exists()


def test_a_project_the_configuration_does_not_describe_falls_back(
    tmp_path: Path, monkeypatch
) -> None:
    """Wiring before writing any seats has nothing better to go on than what is
    installed, and wiring nothing would leave somebody with no gate and no
    explanation."""
    root = tmp_path / "unconfigured"
    root.mkdir()
    project = repo(root)
    monkeypatch.chdir(tmp_path)

    wiring.wire(project, runtimes=(CLAUDE,))

    assert (project / ".claude" / "settings.local.json").exists()


def test_with_nothing_given_the_configuration_decides_not_the_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Halyardception, and it is not a joke at anyone's expense.

    Where you are standing when you run `halyard wire` is almost always the
    Halyard checkout, so a `cwd` default gated Halyard with its own bridge —
    the control plane's every command then went through the hook it was
    serving.
    """
    project = configured_project(tmp_path, ["claude-code"])
    standing_in = tmp_path / "halyard-fleet"
    standing_in.mkdir()
    monkeypatch.chdir(standing_in)
    # Read from the working directory, as the real configuration is.
    monkeypatch.setattr(
        "halyard.core.config_file.find_config", lambda d=None: tmp_path / "halyard.yaml"
    )

    assert wiring.targets(None) == [project]


def test_a_named_project_still_wins(tmp_path: Path, monkeypatch) -> None:
    configured_project(tmp_path, ["claude-code"])
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()

    assert wiring.targets(str(other)) == [other]


def test_with_no_configuration_at_all_it_is_where_you_are(tmp_path: Path, monkeypatch) -> None:
    """Then there is genuinely nothing else to go on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("halyard.core.config_file.find_config", lambda d=None: None)

    assert wiring.targets(None) == [Path.cwd()]


def test_what_is_installed_does_not_decide_when_the_project_is_described(
    tmp_path: Path, monkeypatch
) -> None:
    """The postmortem's rule, pinned.

    On the machine this failed on, `installed()` answered "Antigravity only" —
    Antigravity publishes a command and Claude Code hides its binary in an app
    bundle. The project's own file said `claude-code`, twice. Forced here to
    the same wrong answer, so the assertion is about the code rather than about
    whichever agents happen to be on the machine running the tests.
    """
    project = configured_project(tmp_path, ["claude-code"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "installed", lambda: (ANTIGRAVITY,))

    wiring.wire(project)

    assert (project / ".claude" / "settings.local.json").exists()
    assert not (project / ".agents").exists()


# --- a hooks file that travelled between machines ----------------------------


def elsewhere(script: str) -> dict:
    """A Halyard hook written by a machine that is not this one."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"/Users/somebody-else/checkout/bridge/{script}",
                "timeout": 600,
            }
        ],
        "matcher": CODEX.hooks.matcher,
    }


def test_a_hook_from_another_machine_is_dropped_rather_than_doubled(tmp_path: Path) -> None:
    """The file this is a regression for came off a real project.

    `.codex/hooks.json` is committed in some repositories, so it travels by
    git. Each machine's `wire` correctly decided the other's entry was not its
    own — and appended beside it. The result was two `PreToolUse` groups on one
    matcher, one of them naming a home directory that does not exist here, and
    `doctor` reporting a path that is not on this machine.
    """
    project = repo(tmp_path)
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [elsewhere("hook.sh")]}})
    )

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((codex_dir / "hooks.json").read_text())
    commands = [
        hook["command"] for group in written["hooks"]["PreToolUse"] for hook in group["hooks"]
    ]
    assert commands == [str(wiring.BRIDGE_DIR / "hook.sh")]


def test_the_one_already_here_is_not_duplicated(tmp_path: Path) -> None:
    """A file holding both machines' entries keeps exactly one afterwards.

    Rewriting the dead entry to this machine's path would have left two
    identical hooks where the file already had ours — which is the same
    duplication, one indirection later.
    """
    project = repo(tmp_path)
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    ours = {
        "hooks": [
            {
                "type": "command",
                "command": str(wiring.BRIDGE_DIR / "hook.sh"),
                "timeout": 600,
            }
        ],
        "matcher": CODEX.hooks.matcher,
    }
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [ours, elsewhere("hook.sh")]}})
    )

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((codex_dir / "hooks.json").read_text())
    assert len(written["hooks"]["PreToolUse"]) == 1


def test_somebody_elses_tool_is_still_left_alone(tmp_path: Path) -> None:
    """The narrowness is the whole safety of this.

    Only Halyard's own script names, only in a `bridge/` directory, and only
    when the path does not exist here. A hook belonging to another tool is
    untouched however dead its path looks — removing one somebody meant to
    keep is the expensive mistake.
    """
    project = repo(tmp_path)
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    theirs = {
        "hooks": [{"type": "command", "command": "/opt/other-tool/scripts/hook.sh"}],
        "matcher": "^Bash$",
    }
    (codex_dir / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [theirs]}}))

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((codex_dir / "hooks.json").read_text())
    kept = [hook["command"] for group in written["hooks"]["PreToolUse"] for hook in group["hooks"]]
    assert "/opt/other-tool/scripts/hook.sh" in kept


def test_a_second_halyard_that_is_really_here_stays(tmp_path: Path) -> None:
    """Two checkouts on one machine is a real shape, and both are alive.

    The test is that "not ours" and "cannot run" are different questions: only
    the second one removes anything.
    """
    project = repo(tmp_path)
    other = tmp_path / "second-checkout" / "bridge"
    other.mkdir(parents=True)
    (other / "hook.sh").write_text("#!/bin/sh\n")
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [{"type": "command", "command": str(other / "hook.sh")}],
                            "matcher": CODEX.hooks.matcher,
                        }
                    ]
                }
            }
        )
    )

    wiring.wire(project, runtimes=(CODEX,))

    written = json.loads((codex_dir / "hooks.json").read_text())
    kept = [hook["command"] for group in written["hooks"]["PreToolUse"] for hook in group["hooks"]]
    assert str(other / "hook.sh") in kept
    assert str(wiring.BRIDGE_DIR / "hook.sh") in kept


def test_claude_code_gates_the_question_tool_as_well_as_the_shell(tmp_path: Path) -> None:
    """`AskUserQuestion` rides the same `PreToolUse` hook as the shell tool, so
    one matcher covers both. A second group sharing `hook.sh` would look to the
    wiring like the gate was already installed and rewrite the gate's matcher."""
    project = repo(tmp_path)

    wiring.wire(project, runtimes=(CLAUDE,))

    groups = read(project)["hooks"]["PreToolUse"]
    matchers = [g["matcher"] for g in groups]
    assert matchers == ["Bash|AskUserQuestion"]


def test_a_shell_only_matcher_from_before_is_corrected(tmp_path: Path) -> None:
    """An install wired before questions existed has a bare `Bash` matcher, and
    leaving it means the question tool is never bridged — the quiet kind of
    wrong the matcher-correction exists for."""
    project = repo(tmp_path)
    wiring.wire(project, runtimes=(CLAUDE,))
    settings = project / ".claude" / "settings.local.json"
    written = json.loads(settings.read_text())
    written["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
    settings.write_text(json.dumps(written))

    wiring.wire(project, runtimes=(CLAUDE,))

    assert read(project)["hooks"]["PreToolUse"][0]["matcher"] == "Bash|AskUserQuestion"
