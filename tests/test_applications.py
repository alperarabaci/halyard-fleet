"""Tests for the application catalogue and opening what it lists.

The catalogue is data, so most of what can go wrong is reading it: an entry
somebody wrote badly, a name that is really an alias, a configured application
that should win over the shipped one. Those get the attention.

`desktop` itself is only exercised through doubles. Its whole content is three
macOS commands, and a test that ran them would either open an application on
whoever's machine is running the suite, or be skipped everywhere else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from halyard.applications import catalogue, desktop

# --- what is listed ---------------------------------------------------------


def test_the_shipped_list_carries_the_three_that_were_measured() -> None:
    """Read off a real machine rather than guessed. If one of these ids is ever
    wrong, `/open` reports "not installed" for an application in plain sight."""
    by_name = {app.name: app for app in catalogue.known()}

    assert by_name["claude"].bundle_id == "com.anthropic.claudefordesktop"
    assert by_name["antigravity"].bundle_id == "com.google.antigravity"
    assert by_name["codex"].bundle_id == "com.openai.codex"


def test_the_name_somebody_types_is_rarely_the_applications_own() -> None:
    """Gemini is what Antigravity is to the person typing, and Codex ships as
    ChatGPT. Both directions have to land."""
    assert catalogue.resolve("gemini").name == "antigravity"
    assert catalogue.resolve("antigravity").name == "antigravity"
    assert catalogue.resolve("chatgpt").name == "codex"
    assert catalogue.resolve("codex").name == "codex"


def test_a_name_is_matched_however_it_was_typed() -> None:
    assert catalogue.resolve("  Gemini  ").name == "antigravity"
    assert catalogue.resolve("CLAUDE").name == "claude"


def test_something_not_listed_resolves_to_nothing() -> None:
    """Opencode is the expected next one, and until it is listed the honest
    answer is that this does not know it."""
    assert catalogue.resolve("opencode") is None
    assert catalogue.resolve("") is None


# --- reading the list -------------------------------------------------------


def test_an_entry_is_read_with_its_aliases_and_fallback() -> None:
    found = catalogue.from_yaml(
        "opencode:\n"
        "  bundle_id: com.opencode.desktop\n"
        "  fallback: /Applications/Opencode.app\n"
        "  aliases: [oc, open-code]\n"
    )

    assert found[0].name == "opencode"
    assert found[0].bundle_id == "com.opencode.desktop"
    assert found[0].fallback == Path("/Applications/Opencode.app")
    assert found[0].aliases == ("oc", "open-code")


def test_a_single_alias_written_as_a_string_is_still_an_alias() -> None:
    """YAML lets somebody write `aliases: oc`, and meaning it is obvious."""
    assert catalogue.from_yaml("x:\n  bundle_id: com.x\n  aliases: oc\n")[0].aliases == ("oc",)


def test_an_entry_with_no_bundle_id_is_skipped_not_raised() -> None:
    """One bad line in a list of applications must not cost the other three —
    and nothing here is worth failing to start over."""
    found = catalogue.from_yaml(
        "broken:\n  fallback: /Applications/X.app\nfine:\n  bundle_id: com.fine\n"
    )

    assert [app.name for app in found] == ["fine"]


def test_an_entry_that_is_not_a_mapping_is_skipped() -> None:
    assert catalogue.from_yaml("broken: just a string\nfine:\n  bundle_id: com.fine\n") != []


def test_a_document_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValueError):
        catalogue.from_yaml("- one\n- two\n")


def test_configuration_replaces_a_shipped_entry_by_name(tmp_path: Path) -> None:
    """Replaced whole rather than merged. An application that moved to a new
    bundle id is a different application, and new-id-with-old-fallback would be
    neither of them."""
    (tmp_path / "halyard.yaml").write_text(
        "projects: {}\napplications:\n  codex:\n    bundle_id: com.openai.something-else\n"
    )

    by_name = {app.name: app for app in catalogue.known(tmp_path)}

    assert by_name["codex"].bundle_id == "com.openai.something-else"
    assert by_name["codex"].fallback is None
    # The others are untouched.
    assert by_name["claude"].bundle_id == "com.anthropic.claudefordesktop"


def test_configuration_can_add_one_that_was_never_shipped(tmp_path: Path) -> None:
    """The escape hatch. A new application is four lines of YAML, not a release."""
    (tmp_path / "halyard.yaml").write_text(
        "projects: {}\napplications:\n  opencode:\n    bundle_id: com.opencode.desktop\n"
    )

    assert catalogue.resolve("opencode", tmp_path).bundle_id == "com.opencode.desktop"


def test_a_broken_applications_block_leaves_the_shipped_list_standing(tmp_path: Path) -> None:
    (tmp_path / "halyard.yaml").write_text("projects: {}\napplications:\n  - not\n  - a mapping\n")

    assert {app.name for app in catalogue.known(tmp_path)} >= {"claude", "codex", "antigravity"}


# --- asking the desktop -----------------------------------------------------


#: An application with no fallback, for the checks about what Spotlight said.
#:
#: Not the shipped `codex` entry, whose fallback is `/Applications/ChatGPT.app`
#: — which exists on the machine this was written on and does not exist on CI.
#: A test that reads differently in those two places is measuring the machine.
NOWHERE = catalogue.Application(name="nowhere", bundle_id="com.example.nowhere")


class FakeRun:
    """Stands in for `subprocess.run`, recording what was asked."""

    def __init__(self, **answers: tuple[int, str]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        code, out = self.answers.get(command[0], (0, ""))
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")


@pytest.fixture
def on_a_mac(monkeypatch):
    monkeypatch.setattr(desktop, "available", lambda: True)


def test_running_is_asked_of_the_bundle_id(monkeypatch, on_a_mac) -> None:
    """Not of the process table. A path breaks for an application launched from
    somewhere unexpected, and a name would match a terminal that merely has the
    name in its command line."""
    fake = FakeRun(osascript=(0, "true\n"))
    monkeypatch.setattr(subprocess, "run", fake)
    app = catalogue.resolve("codex")

    assert desktop.running(app) is True
    assert 'application id "com.openai.codex" is running' in fake.commands[0][-1]


def test_a_closed_application_reads_as_closed(monkeypatch, on_a_mac) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(osascript=(0, "false\n")))

    assert desktop.running(catalogue.resolve("codex")) is False


def test_a_question_that_could_not_be_asked_is_not_a_yes(monkeypatch, on_a_mac) -> None:
    """Fails towards "not running", which only ever costs an extra open."""
    monkeypatch.setattr(subprocess, "run", FakeRun(osascript=(1, "")))

    assert desktop.running(catalogue.resolve("codex")) is False


def test_a_missing_osascript_is_answered_rather_than_raised(monkeypatch, on_a_mac) -> None:
    def explode(*args, **kwargs):
        raise OSError("no such tool")

    monkeypatch.setattr(subprocess, "run", explode)

    assert desktop.running(NOWHERE) is False
    assert desktop.find(NOWHERE) is None


def test_where_it_is_installed_comes_from_spotlight(monkeypatch, on_a_mac, tmp_path) -> None:
    there = tmp_path / "ChatGPT.app"
    there.mkdir()
    monkeypatch.setattr(subprocess, "run", FakeRun(mdfind=(0, f"{there}\n")))

    assert desktop.find(NOWHERE) == there


def test_a_spotlight_answer_pointing_nowhere_is_not_taken(monkeypatch, on_a_mac) -> None:
    """A stale index names applications that were deleted months ago."""
    monkeypatch.setattr(subprocess, "run", FakeRun(mdfind=(0, "/Applications/Gone.app\n")))

    assert desktop.find(NOWHERE) is None


def test_the_declared_fallback_answers_when_spotlight_does_not(
    monkeypatch, on_a_mac, tmp_path
) -> None:
    """Indexing turned off is a real configuration, and "not installed" would be
    a lie there."""
    there = tmp_path / "ChatGPT.app"
    there.mkdir()
    monkeypatch.setattr(subprocess, "run", FakeRun(mdfind=(0, "")))
    app = catalogue.Application(name="codex", bundle_id="com.openai.codex", fallback=there)

    assert desktop.find(app) == there


def test_opening_asks_by_bundle_id(monkeypatch, on_a_mac) -> None:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    assert desktop.open_(catalogue.resolve("gemini")) is True
    assert fake.commands[0] == ["open", "-b", "com.google.antigravity"]


def test_an_open_that_failed_says_so(monkeypatch, on_a_mac) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(open=(1, "")))

    assert desktop.open_(catalogue.resolve("claude")) is False


def test_nothing_is_run_at_all_off_a_mac(monkeypatch) -> None:
    """Said honestly rather than pretended at with something that would not
    work. `open` and `osascript` are what this is."""
    monkeypatch.setattr(desktop, "available", lambda: False)

    def explode(*args, **kwargs):
        raise AssertionError("nothing should be run")

    monkeypatch.setattr(subprocess, "run", explode)
    app = catalogue.resolve("claude")

    assert desktop.find(app) is None
    assert desktop.running(app) is False
    assert desktop.open_(app) is False
