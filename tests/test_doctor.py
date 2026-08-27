"""What `halyard sessions` prints, and where it gets it from.

One mistake has now appeared twice in this codebase: recovering a directory by
decoding the name of the directory the transcripts sit in. That name is a lossy
encoding — every separator became a dash — so `halyard-fleet` and
`halyard/fleet` encode identically and nothing distinguishes them afterwards.

It was fixed in session lookup and left in this listing, which is the output the
README tells people to copy into `.env`.
"""

from __future__ import annotations

import json
from pathlib import Path

from halyard import doctor


def transcript(root: Path, encoded: str, *, name: str, cwd: str, chosen: bool = True) -> Path:
    """A transcript where the recorded cwd disagrees with the directory name."""
    directory = root / encoded
    directory.mkdir(parents=True)
    path = directory / "abc123.jsonl"
    path.write_text(
        json.dumps({"type": "user", "cwd": cwd, "sessionId": "abc123"})
        + "\n"
        + json.dumps(
            {"type": "custom-title", "customTitle": name}
            if chosen
            else {"type": "ai-title", "aiTitle": name}
        )
        + "\n"
    )
    return path


def test_the_listed_directory_comes_from_the_transcript(tmp_path, monkeypatch, capsys) -> None:
    """A dash in a directory name must survive being listed.

    `-Users-me-code-halyard-fleet` is what a session in `.../code/halyard-fleet`
    is filed under. Turning the dashes back into separators produces
    `.../code/halyard/fleet`, which is a directory that does not exist — printed
    beside a name somebody is about to copy into a config file.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".claude" / "projects"
    transcript(
        root,
        "-Users-me-code-halyard-fleet",
        name="alpha-engine-driver",
        cwd="/Users/me/code/halyard-fleet",
    )

    assert doctor.sessions() == 0

    printed = capsys.readouterr().out
    assert "/Users/me/code/halyard-fleet" in printed
    assert "halyard/fleet" not in printed


def test_a_long_path_is_printed_whole(tmp_path, monkeypatch, capsys) -> None:
    """It used to be cut to the last 60 characters, mid-directory-name.

    Which produced things like `mmer/Documents/...` — a path that looks like a
    path and is not one, in a listing whose only job is to be copied.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    deep = "/Users/me/Documents/dev/ai/agent-platform/investment/alpha-engine"
    transcript(
        tmp_path / ".claude" / "projects",
        "-Users-me-Documents-dev-ai-agent-platform-investment-alpha-engine",
        name="alpha-engine-navigator",
        cwd=deep,
    )

    doctor.sessions()

    assert deep in capsys.readouterr().out


def test_a_session_with_no_recorded_directory_says_so(tmp_path, monkeypatch, capsys) -> None:
    """Silence would read as "no directory needed", which is not the case."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    transcript(tmp_path / ".claude" / "projects", "-Users-me-code-thing", name="seat", cwd="")

    doctor.sessions()

    assert "not recorded" in capsys.readouterr().out


def test_project_root_is_the_repository_root(tmp_path) -> None:
    """Measured: a session in a subdirectory is gated from the top of its repo."""
    (tmp_path / ".git").mkdir()
    inside = tmp_path / "web" / "src"
    inside.mkdir(parents=True)

    assert doctor.project_root(inside) == tmp_path


def test_without_a_repository_a_directory_stands_alone(tmp_path) -> None:
    """And measured the other way: with no `.git` above it, nothing inherits."""
    inside = tmp_path / "web" / "src"
    inside.mkdir(parents=True)

    assert doctor.project_root(inside) == inside


def test_a_name_claude_invented_is_marked(tmp_path, monkeypatch, capsys) -> None:
    """Only a name a person chose is stable.

    Claude rewrites a generated title as the conversation moves, so a seat
    pointed at one routes correctly the day it is copied and then stops without
    an error — which reads as Halyard losing messages rather than as a name
    having moved underneath it. This listing exists to be copied from, so it
    has to say which names are safe to copy.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".claude" / "projects"
    transcript(root, "-a", name="alpha-engine-driver", cwd="/a", chosen=True)
    transcript(root, "-b", name="Run echo hello command", cwd="/b", chosen=False)

    doctor.sessions()

    printed = capsys.readouterr().out
    chosen_line = next(ln for ln in printed.splitlines() if "alpha-engine-driver" in ln)
    invented_line = next(ln for ln in printed.splitlines() if "Run echo hello" in ln)
    assert "auto-titled" not in chosen_line
    assert "auto-titled" in invented_line
    assert "Rename" in printed


def test_no_warning_when_every_name_was_chosen(tmp_path, monkeypatch, capsys) -> None:
    """A warning that is always present is one nobody reads."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    transcript(
        tmp_path / ".claude" / "projects",
        "-a",
        name="alpha-engine-driver",
        cwd="/a",
        chosen=True,
    )

    doctor.sessions()

    assert "auto-titled" not in capsys.readouterr().out


# --- reading Antigravity's hooks file -----------------------------------------

ANTIGRAVITY_FILE = {
    "halyard": {
        "PreToolUse": [{"matcher": "run_command", "hooks": [{"command": "/b/hook.sh"}]}],
        "Stop": [{"command": "/b/relay.py"}],
    }
}


def antigravity_file(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(config))
    return path


def test_antigravitys_own_shape_is_read(tmp_path: Path) -> None:
    """Both shapes at once, which is the part worth asserting.

    `PreToolUse` is wrapped in a `matcher`/`hooks` group and `Stop` is a flat
    handler. A parser that assumes either one finds half the file, and finding
    none of it reports an ungated project — whose obvious remedy is to go and
    wire a second gate on top of the one already there.
    """
    path = antigravity_file(tmp_path, ANTIGRAVITY_FILE)

    found = doctor._hook_commands(path, tmp_path, "antigravity")

    assert sorted(found) == [("PreToolUse", "/b/hook.sh"), ("Stop", "/b/relay.py")]


def test_a_disabled_hook_is_reported(tmp_path: Path) -> None:
    """`"enabled": false` runs nothing while the file still reads as wired:
    the events are there, the commands are there, the paths are right."""
    path = antigravity_file(
        tmp_path, {"halyard": {**ANTIGRAVITY_FILE["halyard"], "enabled": False}}
    )

    assert doctor._disabled_hooks(path) == ["halyard"]


def test_an_enabled_hook_is_not_reported(tmp_path: Path) -> None:
    """Absent means enabled — the default is `true`, so a file that never
    mentions it must not be read as switched off."""
    assert doctor._disabled_hooks(antigravity_file(tmp_path, ANTIGRAVITY_FILE)) == []


def test_another_tools_hooks_are_read_too(tmp_path: Path) -> None:
    """Every name in the file gates the project, not just this install's."""
    path = antigravity_file(
        tmp_path,
        {**ANTIGRAVITY_FILE, "safety": {"PreToolUse": [{"hooks": [{"command": "/o/check.sh"}]}]}},
    )

    assert ("PreToolUse", "/o/check.sh") in doctor._hook_commands(path, tmp_path, "antigravity")


# --- a CLI that is present and cannot sign in ---------------------------------


def _claude_check(
    monkeypatch,
    *,
    found: str | None,
    signed: bool | None,
    token: str | None = "sk-ant-oat-configured",
    method: str | None = None,
    api_key: str | None = None,
):
    """The Claude Code spec's own availability check, with the answers forced.

    A token is supplied by default so most cases describe the arrangement this
    is meant to be run in — a control plane with a credential of its own, rather
    than one riding on a login somebody made at a keyboard.
    """
    from halyard.agents import registry

    monkeypatch.setattr("halyard.agents.claude_code.runner.find_claude_binary", lambda *_a: found)
    monkeypatch.setattr("halyard.agents.claude_code.runner.signed_in", lambda *_a: signed)
    monkeypatch.setattr("halyard.agents.claude_code.runner.auth_method", lambda *_a: method)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if api_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    return registry.get("claude-code").check_available(claude_oauth_token=token)


def test_a_cli_that_cannot_sign_in_is_a_failure(monkeypatch) -> None:
    """The gap that let a Mac mini look healthy and deliver nothing.

    The binary was there and `doctor` reported it, while every message failed
    with the CLI's own "Not logged in · Please run /login" — a line that was
    being written to stdout and thrown away. Present is not the same as usable.
    """
    found = _claude_check(monkeypatch, found="/bin/claude", signed=False)

    assert [level for level, _ in found] == ["ok", "ok", "fail", ""]
    assert "not signed in" in found[2][1]
    assert "auth login" in found[3][1], "say the command, not just the problem"


def test_the_command_it_prints_can_be_pasted(monkeypatch) -> None:
    """The path is almost always `~/Library/Application Support/...`.

    An instruction with an unquoted space in it is not an instruction — and
    this is the path people need, because `claude` is frequently absent from
    PATH on a machine that has it: the binary lives inside the desktop app's
    bundle, which is how Halyard finds it and why the shell does not.
    """
    found = _claude_check(
        monkeypatch, found="/Users/x/Library/Application Support/Claude/claude", signed=False
    )

    assert '"/Users/x/Library/Application Support/Claude/claude" auth login' in found[3][1]


def test_a_signed_in_cli_says_nothing_extra(monkeypatch) -> None:
    """A clean check should be one line, not a paragraph about what is fine."""
    found = _claude_check(monkeypatch, found="/bin/claude", signed=True)

    assert [level for level, _ in found] == ["ok", "ok"]


def test_not_being_able_to_tell_is_not_a_failure(monkeypatch) -> None:
    """`None` means the question could not be asked — an old CLI without the
    subcommand, a timeout. Reporting that as a failure would send somebody to
    re-authenticate something that was never signed out."""
    found = _claude_check(monkeypatch, found="/bin/claude", signed=None)

    assert [level for level, _ in found] == ["ok", "ok", "warn"]


# --- a hooks file that will follow the repository elsewhere -------------------


def _repo_with_hooks(tmp_path: Path, *, committed: bool, ignored: bool) -> Path:
    """A real git repository, because the question is git's to answer."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=False)

    git("init")
    (tmp_path / ".codex").mkdir()
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.write_text('{"hooks": {}}')
    if ignored:
        (tmp_path / ".gitignore").write_text(".codex/hooks.json\n")
    if committed:
        git("add", "-f", ".codex/hooks.json")
        git("-c", "user.email=a@b", "-c", "user.name=c", "commit", "-m", "x")
    return hooks


def test_a_committed_hooks_file_is_reported(tmp_path: Path) -> None:
    """It holds absolute paths, so committing it sends one machine's home
    directory to every other checkout — which is how a project ended up with
    two PreToolUse groups on one matcher, half of them pointing nowhere."""
    hooks = _repo_with_hooks(tmp_path, committed=True, ignored=False)

    assert doctor._travels_between_machines(hooks) == "committed"


def test_an_unignored_hooks_file_is_reported_before_it_is_committed(tmp_path: Path) -> None:
    """The next `git add -A` is all it takes, and by then it is on the other
    machine."""
    hooks = _repo_with_hooks(tmp_path, committed=False, ignored=False)

    assert doctor._travels_between_machines(hooks) == "not ignored"


def test_an_ignored_hooks_file_says_nothing(tmp_path: Path) -> None:
    """The cause is fixed; there is nothing to warn about."""
    hooks = _repo_with_hooks(tmp_path, committed=False, ignored=True)

    assert doctor._travels_between_machines(hooks) is None


def test_a_file_outside_a_repository_cannot_travel_this_way(tmp_path: Path) -> None:
    """No repository, no journey. Warning here would be noise on every machine
    that keeps its projects outside git."""
    loose = tmp_path / "hooks.json"
    loose.write_text('{"hooks": {}}')

    assert doctor._travels_between_machines(loose) is None


# --- the credential these turns actually run on ------------------------------
#
# Measured twice, four days apart: remote work stopped with "OAuth session
# expired and could not be refreshed" until somebody signed in at the desk. A
# control plane whose whole purpose is to work while nobody is at the desk
# cannot depend on a login that only a person at the desk can renew.


def test_running_on_the_desktop_login_is_a_warning(monkeypatch) -> None:
    """It works until it does not, and the failure lands while you are away."""
    found = _claude_check(monkeypatch, found="/bin/claude", signed=True, token=None)

    levels = [level for level, _ in found]
    assert "warn" in levels
    said = " ".join(text for _, text in found)
    assert "expires" in said
    # The way out, spelled out where it is needed rather than in a document.
    assert "setup-token" in said
    assert "HALYARD_CLAUDE_OAUTH_TOKEN" in said


def test_it_names_what_the_cli_is_falling_back_to(monkeypatch) -> None:
    """Which credential, not just that there is a problem — the two failures
    look identical from outside and are fixed differently."""
    found = _claude_check(
        monkeypatch, found="/bin/claude", signed=True, token=None, method="claudeai"
    )

    assert any("authMethod=claudeai" in text for _, text in found)


def test_an_api_key_silently_outranks_the_token(monkeypatch) -> None:
    """ANTHROPIC_API_KEY ranks above the token *and* bills the API rather than
    the subscription, so an inherited one changes both which credential is used
    and who pays. Nothing else would say so."""
    found = _claude_check(
        monkeypatch, found="/bin/claude", signed=True, token="sk-ant-oat-x", api_key="sk-ant-api-y"
    )

    warning = [text for level, text in found if level == "warn"]
    assert warning and "outranks" in warning[0]
    assert "bills" in warning[0]


# --- the service's own log, which launchd holds and Halyard cannot rotate ----


def test_the_service_log_is_named_even_when_it_is_small(tmp_path: Path) -> None:
    """Silence would leave a file growing all year that nothing ever names."""
    log = tmp_path / "halyard-service.log"
    log.write_bytes(b"x" * 1000)

    lines, problems = doctor.check_service_log(log)

    assert problems == 0
    assert any(str(log) in line for line in lines)


def test_a_large_service_log_says_how_to_empty_it(tmp_path: Path) -> None:
    """Truncating in place is the safe move: launchd holds the file open, so
    renaming it leaves the service writing where nobody can find it."""
    log = tmp_path / "halyard-service.log"
    log.write_bytes(b"x" * (doctor.SERVICE_LOG_WARN_BYTES + 1))

    lines, problems = doctor.check_service_log(log)

    assert problems == 1
    printed = "\n".join(lines)
    assert f": > {log}" in printed
    assert "cannot rotate" in printed


def test_a_growing_service_log_is_mentioned_before_it_is_a_problem(tmp_path: Path) -> None:
    log = tmp_path / "halyard-service.log"
    log.write_bytes(b"x" * (doctor.SERVICE_LOG_NOTE_BYTES + 1))

    lines, problems = doctor.check_service_log(log)

    # Worth saying, not worth failing over.
    assert problems == 0
    assert any(": >" in line for line in lines)


def test_no_service_log_still_says_where_it_would_be(tmp_path: Path) -> None:
    """Not installed as a service is not a problem — but that path is where the
    `git pull` and `uv sync` output goes, and nothing else says so."""
    missing = tmp_path / "not-there.log"

    lines, problems = doctor.check_service_log(missing)

    assert problems == 0
    assert any(str(missing) in line for line in lines)
