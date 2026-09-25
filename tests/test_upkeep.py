"""Tests for `halyard.upkeep` — what Halyard does about itself — and its one
job so far, `runs-advice`: the evidence from the log, judged again by the
service's own rules, and a model's proposals checked and counted by Halyard."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from halyard import upkeep
from halyard.core import grants, trusted_runs
from halyard.core.audit import AuditAction, AuditRecord, SqliteAuditSink
from halyard.core.config_file import projects_from_yaml
from halyard.upkeep import runs_advice

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
ON = grants.Rules(reads=True)


@pytest.fixture
def project(tmp_path: Path):
    root = tmp_path / "alpha-engine"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    [described] = projects_from_yaml(
        f"projects:\n  alpha-engine:\n    path: {root}\n    runs:\n      - make lint\n"
    )
    return described


def logged(database: Path, *cards: dict) -> None:
    """Cards written the way the service writes them, each with how it ended."""

    async def write() -> None:
        sink = SqliteAuditSink(database)
        await sink.open()
        for number, card in enumerate(cards):
            at = card.pop("at", NOW - timedelta(hours=1))
            ended = card.pop("ended", ("allow", "user"))
            project = card.pop("project", "alpha-engine")
            request = f"req_{number}"
            await sink.write(
                AuditRecord(
                    action=AuditAction.APPROVAL_REQUESTED,
                    recorded_at=at,
                    request_id=request,
                    project=project,
                    detail={"tool": "Bash", **card},
                )
            )
            if ended is not None:
                decision, reason = ended
                await sink.write(
                    AuditRecord(
                        action=AuditAction.APPROVAL_RESOLVED,
                        recorded_at=at + timedelta(seconds=5),
                        request_id=request,
                        project=project,
                        detail={"decision": decision, "reason": reason},
                    )
                )
        await sink.close()

    asyncio.run(write())


def gathered(database: Path, project, rules: grants.Rules = ON, **more):
    return runs_advice.gather(database, project, rules, days=14, now=NOW, **more)


# --- the block ---------------------------------------------------------------------


def test_a_job_has_its_defaults_unless_the_block_says_otherwise(tmp_path: Path) -> None:
    jobs = upkeep.from_yaml(
        "upkeep:\n  runs-advice:\n    model: opus\n    days: 30\n    prompt: mine.md\n",
        base=tmp_path,
    )

    job = jobs["runs-advice"]
    assert (job.model, job.effort, job.days, job.timeout) == ("opus", None, 30, 600.0)
    assert job.prompt == tmp_path / "mine.md"
    assert upkeep.from_yaml("settings: {}") == {}


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("upkeep: [runs-advice]", "must be a mapping"),
        ("upkeep:\n  cleanup: {}\n", "is not a job"),
        ("upkeep:\n  runs-advice:\n    modle: opus\n", "unknown setting"),
        ("upkeep:\n  runs-advice:\n    days: 0\n", "above zero"),
    ],
)
def test_a_block_it_cannot_honour_is_refused_with_why(text: str, said: str) -> None:
    with pytest.raises(ValueError, match=said):
        upkeep.from_yaml(text)


# --- the evidence ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "family"),
    [
        ("cd /x && uv run pytest -q tests", "uv run pytest"),
        ("UV_CACHE_DIR=/tmp/u uv run --no-sync pytest", "uv run pytest"),
        ("make test-fast 2>&1 | tail", "make test-fast"),
        ("git log --oneline", "git log"),
        ("python3 -c 'x'", "python3"),
        ("echo $(date)", "echo (not parsed)"),
    ],
)
def test_a_family_is_what_a_command_is(command: str, family: str) -> None:
    assert runs_advice.family_of(command) == family


def test_each_card_is_judged_again_by_todays_rules(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(
        database,
        {"command": "git status"},
        {"command": "uv run pytest -q", "ended": ("deny", "user")},
        {"command": "make lint"},
        {"command": "cat .env.local TOKEN=***", "ended": ("deny", "timeout")},
        {"command": "ls", "redacted": True, "ended": None},
    )

    found = gathered(database, project)

    assert [(c.family, c.outcome, c.today) for c in found.cards] == [
        ("git status", "allowed", "passes"),
        ("uv run pytest", "denied", "card"),
        ("make lint", "allowed", "passes"),
        ("cat", "timed out", "undetermined"),
        ("ls", "unanswered", "undetermined"),
    ]


def test_with_the_setting_off_nothing_passes(tmp_path: Path, project) -> None:
    """The question the service asks, with this machine's settings."""
    database = tmp_path / "halyard.db"
    logged(database, {"command": "git status"})

    [card] = gathered(database, project, grants.Rules(reads=False)).cards

    assert card.today == "card"


def test_a_commit_refused_outright_is_said_so(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(database, {"command": "git commit -m x"})

    [card] = gathered(database, project, refuse_commits=True).cards

    assert card.today == "refused"


def test_where_it_ran_is_used_when_the_record_says(tmp_path: Path, project) -> None:
    """Before 2026-09-25 a card did not say, and is judged from the root."""
    database = tmp_path / "halyard.db"
    root = str(project.path)
    logged(
        database,
        {"command": "cat ../x", "cwd": f"{root}/src", "project_dir": root},
        {"command": "cat ../x"},
    )

    exact, guessed = gathered(database, project).cards

    assert (exact.approximate, exact.today) == (False, "passes")
    assert (guessed.approximate, guessed.today) == (True, "card")


def test_only_this_projects_shell_cards_in_the_window(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(
        database,
        {"command": "ls"},
        {"command": "ls", "project": "other"},
        {"command": "ls", "at": NOW - timedelta(days=30)},
        {"command": "Write src/a.py", "tool": "Write"},
    )

    found = gathered(database, project)

    assert len(found.cards) == 1
    assert found.others == 1


def test_what_the_project_trusts_is_both_places(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(database, {"command": "uv run pytest -q"})
    trusted_runs.add(database, "alpha-engine", "uv run pytest *", by="test")

    found = gathered(database, project)

    assert found.trusted == (("make lint", "halyard.yaml"), ("uv run pytest *", "halyard rules"))
    assert found.cards[0].today == "passes"


def test_gathering_writes_nothing(tmp_path: Path, project) -> None:
    """Opened read-only: not even the table a read elsewhere would create."""
    database = tmp_path / "halyard.db"
    logged(database, {"command": "ls"})
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    gathered(database, project)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_no_database_is_no_evidence(tmp_path: Path, project) -> None:
    found = gathered(tmp_path / "halyard.db", project)

    assert found.cards == ()
    assert not (tmp_path / "halyard.db").exists()


def test_the_evidence_is_bounded_and_says_what_it_left_out(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(database, *({"command": f"tool{n} run"} for n in range(5)))

    text = runs_advice.render(gathered(database, project), most=3)

    assert "3 of 5 families shown; 2 families with 2 cards left out" in text
    assert "Shell commands that came as cards: 5 — allowed 5" in text


def test_the_setting_being_off_is_said(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(database, {"command": "make x"})

    text = runs_advice.render(gathered(database, project, grants.Rules()))

    assert "HALYARD_ALLOW_RISK_AT_OR_BELOW is off" in text


# --- the answer --------------------------------------------------------------------


def answered(*proposals: dict, keep: tuple = ()) -> str:
    body = json.dumps({"proposals": list(proposals), "keep_asking": list(keep)})
    return f"Here you are:\n```json\n{body}\n```"


def test_a_proposal_is_checked_and_counted_by_halyard(tmp_path: Path, project) -> None:
    """The counts are Halyard's, from the log, never the model's."""
    database = tmp_path / "halyard.db"
    logged(
        database,
        {"command": "uv run pytest -q tests"},
        {"command": "uv run pytest src", "ended": ("deny", "timeout")},
        {"command": "uv run python x.py"},
    )
    found = gathered(database, project)

    advice = runs_advice.advise(
        answered(
            {"entry": "uv run pytest *", "why": "tests", "spared": 999},
            {"entry": "bash *", "why": "everything"},
            {"entry": "make lint", "why": "lint"},
        ),
        found,
    )

    pytest_entry, bash_entry, lint_entry = advice.proposals
    assert (pytest_entry.spared, pytest_entry.spared_uneasy) == (2, 1)
    assert bash_entry.refused and "could run anything" in bash_entry.refused
    assert lint_entry.already


def test_what_the_person_reads_carries_a_line_halyard_quoted(tmp_path: Path, project) -> None:
    database = tmp_path / "halyard.db"
    logged(database, {"command": "uv run pytest -q"})
    found = gathered(database, project)
    advice = runs_advice.advise(
        answered(
            {"entry": "uv run pytest *", "why": "tests"},
            {"entry": "sudo x", "why": "x"},
            keep=({"family": "uv run python", "why": "code"},),
        ),
        found,
    )

    text = runs_advice.render_advice(found, advice)

    assert "halyard rules add alpha-engine 'uv run pytest *'" in text
    assert "would have spared 1 of the cards" in text
    assert "not an entry:" in text and "halyard rules add alpha-engine 'sudo x'" not in text
    assert "uv run python — code" in text


def test_an_answer_of_another_shape_is_not_taken(tmp_path: Path, project) -> None:
    found = gathered(tmp_path / "halyard.db", project)

    assert runs_advice.advise("I think you should trust pytest.", found) is None
    assert runs_advice.advise('{"proposals": "pytest"}', found) is None


# --- the command -----------------------------------------------------------------


class Answering:
    """A runtime that takes a turn with no tools, and says what it is told to."""

    answers_without_tools = True

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict]] = []

    async def ask(self, text: str, **given) -> str | None:
        self.asked.append((text, given))
        return self.answer


class KeepingItsTools(Answering):
    answers_without_tools = False


@pytest.fixture
def machine(tmp_path: Path, project, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine of its own: this database, this project, never the service."""
    from types import SimpleNamespace

    from halyard import upkeep_cli

    database = tmp_path / "halyard.db"
    settings = SimpleNamespace(
        db_path=database,
        allow_risk_at_or_below="low",
        refuse_agent_commits=False,
        inspection_model=None,
        inspection_effort=None,
    )
    monkeypatch.setattr(upkeep_cli, "_settings", lambda: settings)
    monkeypatch.setattr(upkeep_cli, "_project", lambda name: project)
    monkeypatch.setattr(upkeep_cli, "_rules", lambda given: ON)
    monkeypatch.setattr(
        upkeep_cli.upkeep, "load", lambda name: upkeep.Job(name, model="opus", timeout=5)
    )
    return database


def asking(monkeypatch: pytest.MonkeyPatch, runner) -> None:
    from halyard import upkeep_cli

    monkeypatch.setattr(upkeep_cli, "_runner", lambda settings: runner)


def run(*args: str) -> int:
    from halyard import upkeep_cli

    return upkeep_cli.main(["runs-advice", "alpha-engine", *args])


def test_the_evidence_alone_starts_no_model(machine: Path, monkeypatch, capsys) -> None:
    logged(machine, {"command": "uv run pytest -q"})
    monkeypatch.setattr(
        "halyard.upkeep_cli._runner", lambda settings: pytest.fail("a model was started")
    )

    assert run("--evidence") == 0
    assert "Still cards today" in capsys.readouterr().out


def test_a_runtime_that_keeps_its_tools_is_not_asked(machine: Path, monkeypatch, capsys) -> None:
    """Codex puts a system prompt in front of the text; opencode keeps its
    tools. The evidence goes only to a turn that can read nothing else."""
    logged(machine, {"command": "uv run pytest -q"})
    runner = KeepingItsTools(answered())
    asking(monkeypatch, runner)

    assert run() == 1
    assert runner.asked == []
    assert "without tools" in capsys.readouterr().err


def test_no_answer_ends_the_command(machine: Path, monkeypatch, capsys) -> None:
    """A model that fails or runs out of time ends it — nothing waits."""
    logged(machine, {"command": "uv run pytest -q"})
    asking(monkeypatch, Answering(None))

    assert run() == 1
    assert "no answer" in capsys.readouterr().err


def test_proposals_come_back_checked(machine: Path, monkeypatch, capsys) -> None:
    logged(machine, {"command": "uv run pytest -q"})
    runner = Answering(answered({"entry": "uv run pytest *", "why": "tests"}))
    asking(monkeypatch, runner)

    assert run() == 0

    assert "halyard rules add alpha-engine 'uv run pytest *'" in capsys.readouterr().out
    [(text, given)] = runner.asked
    assert "uv run pytest" in text
    assert given["system"] == runs_advice.PROMPT.read_text(encoding="utf-8")
    assert (given["model"], given["purpose"]) == ("opus", "upkeep runs-advice")
    assert given["cwd"] != machine.parent / "alpha-engine", "run nowhere near the project"


def test_an_answer_of_another_shape_adds_nothing(machine: Path, monkeypatch, capsys) -> None:
    logged(machine, {"command": "uv run pytest -q"})
    asking(monkeypatch, Answering("Just trust pytest."))

    assert run() == 1
    said = capsys.readouterr()
    assert "Just trust pytest." in said.out
    assert "not in the shape" in said.err


def test_nothing_still_a_card_asks_no_model(machine: Path, monkeypatch, capsys) -> None:
    logged(machine, {"command": "git status"})
    monkeypatch.setattr(
        "halyard.upkeep_cli._runner", lambda settings: pytest.fail("a model was started")
    )

    assert run() == 0
    assert "nothing to propose" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["runs-advice"],
        ["tidy", "x"],
        ["runs-advice", "a", "--days", "0"],
        ["runs-advice", "a", "-x"],
    ],
)
def test_what_it_does_not_take_is_refused_with_how_to_ask(machine: Path, capsys, args) -> None:
    from halyard import upkeep_cli

    assert upkeep_cli.main(args) == 2
    assert "usage: halyard upkeep" in capsys.readouterr().err


def test_only_a_runtime_that_promises_it_answers_without_tools() -> None:
    """Asked for by the capability, never by the runtime's name."""
    from halyard.agents.claude_code.runner import ClaudeCodeRunner
    from halyard.agents.codex.runner import CodexRunner
    from halyard.agents.opencode.runner import OpencodeRunner

    assert ClaudeCodeRunner.answers_without_tools is True
    assert not getattr(CodexRunner, "answers_without_tools", False)
    assert not getattr(OpencodeRunner, "answers_without_tools", False)
