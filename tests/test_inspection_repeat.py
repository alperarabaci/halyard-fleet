"""Tests for `halyard.inspections.repeat` and `halyard inspect` — a kept run
given again to another model, and the runs side by side."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from halyard import inspect_cli, inspections
from halyard.core import usage
from halyard.core.config_file import Project
from halyard.inspections import repeat as repeating

ORIGINAL = "5f0c9a2e-1111-4a4a-9b9b-000000000001"
INPUT = "# proof\n\n---\n\nEnvelope:\n- HEAD: 2cdcab9c\n\nText to check:\n\n42 passed"
AT = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
NOW = ["Work item: alpha-engine#386", "HEAD: 2cdcab9c", "Content: 5df40c87aff3 (write-tree)"]


def kept(
    database: Path,
    session: str = ORIGINAL,
    *,
    later: int = 0,
    model: str = "sonnet",
    head: str | None = "2cdcab9c",
    content: str | None = "5df40c87aff3",
    answer: str | None = "proof · no finding",
    repeat_of: str | None = None,
    effort: str | None = None,
) -> None:
    """A run as the work keeps one — or, with `repeat_of`, as a repeat does."""
    inspections.record.keep(
        database,
        inspections.Kept(
            session=session,
            at=AT + timedelta(hours=later),
            name="proof",
            path=Path("NOTES/proof.md"),
            version="3e8c847",
            transition="close",
            model=model,
            asked=INPUT,
            context=(
                "Work item: alpha-engine#386",
                *([f"HEAD: {head}"] if head else []),
                *([f"Content: {content} (write-tree)"] if content else []),
            ),
            note="delivery",
            answer=answer,
            why="" if answer else "the model did not answer",
            finding=None,
            took=41.5,
            effort=effort,
        ),
        project="alpha-engine",
        work="alpha-engine#386",
        runtime="claude-code",
        workflow_run="alpha-engine#386 2026-09-23T17:00:00+00:00",
        step="developed",
        phase=2,
        round=1,
        experimental=repeat_of is not None,
        repeat_of=repeat_of,
    )


class Asking:
    """An `Asker` that answers from a script and remembers how it was asked."""

    def __init__(self, says: str | None = "proof · no finding", raises: Exception | None = None):
        self.says = says
        self.raises = raises
        self.asked: list[dict] = []

    async def ask(self, text: str, **how) -> str | None:
        self.asked.append({"text": text, **how})
        if self.raises is not None:
            raise self.raises
        return self.says


async def repeated(database: Path, tmp_path: Path, asker: Asking, **kwargs) -> repeating.Row | None:
    original = repeating.find(database, ORIGINAL)
    assert original is not None
    return await repeating.repeat(
        original,
        asker=asker,
        model="haiku",
        runtime="claude-code",
        project=tmp_path,
        context=NOW,
        findings=("status: candidate",),
        database=database,
        **kwargs,
    )


# --- a repeat ----------------------------------------------------------------


async def test_a_repeat_gives_another_model_the_same_input_word_for_word(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database)
    asker = Asking(says="proof · status: candidate")

    again = await repeated(database, tmp_path, asker, session="sess-again")

    [asked] = asker.asked
    assert asked["text"] == INPUT
    assert (asked["model"], asked["cwd"], asked["name"]) == ("haiku", tmp_path, "proof")
    assert (asked["edits"], asked["session_id"]) == (False, "sess-again")
    assert again is not None
    assert (again.id, again.experimental, again.repeat_of) == ("sess-again", True, ORIGINAL)
    assert (again.model, again.runtime, again.input) == ("haiku", "claude-code", INPUT)
    assert (again.answer, again.outcome, again.finding) == (
        "proof · status: candidate",
        "answered",
        "status: candidate",
    )
    # The instructions it ran are the original's: they are inside the input.
    assert (again.file, again.file_version) == ("NOTES/proof.md", "3e8c847")
    # Where the files stood as it ran, for a comparison to weigh.
    assert (again.head, again.content) == ("2cdcab9c", "5df40c87aff3")


async def test_a_repeat_ran_for_no_transition_and_no_step(tmp_path: Path) -> None:
    """Those are the original's, one `repeat_of` away. A repeat counted as a
    workflow step's would count that step twice."""
    database = tmp_path / "halyard.db"
    kept(database)

    again = await repeated(database, tmp_path, Asking())

    assert again is not None
    assert (again.transition, again.workflow_run, again.step, again.phase, again.round) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert (again.project, again.work, again.note) == (
        "alpha-engine",
        "alpha-engine#386",
        "delivery",
    )


@pytest.mark.parametrize(
    ("asker", "why"),
    [
        (Asking(says=None), "the model did not answer"),
        (Asking(raises=inspections.StoppedError("stopped by tg:4242")), "stopped by tg:4242"),
        (Asking(raises=RuntimeError("no model today")), "it failed: no model today"),
    ],
)
async def test_a_repeat_with_no_answer_is_kept_and_says_why(
    tmp_path: Path, asker: Asking, why: str
) -> None:
    database = tmp_path / "halyard.db"
    kept(database)

    again = await repeated(database, tmp_path, asker)

    assert again is not None
    assert (again.answer, again.outcome, again.finding) == (None, "unmeasured", None)
    assert again.why == why


# --- finding the runs ----------------------------------------------------------


def test_recent_is_the_work_s_runs_newest_first_with_their_repeats_counted(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database, "run-a")
    kept(database, "run-b", later=1)
    kept(database, "again-1", later=2, repeat_of="run-a")
    kept(database, "again-2", later=3, repeat_of="run-a")

    assert [run.id for run in repeating.recent(database)] == ["run-b", "run-a"]
    assert repeating.repeats(database, ["run-a", "run-b"]) == {"run-a": 2}


def test_an_id_can_be_cut_to_a_start_only_it_has(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database, "5f0c9a2e-aaaa")
    kept(database, "5f0c1b3d-bbbb")

    found = repeating.find(database, "5f0c9")

    assert found is not None and found.id == "5f0c9a2e-aaaa"
    with pytest.raises(ValueError, match="2 runs"):
        repeating.find(database, "5f0c")
    assert repeating.find(database, "0000") is None
    # A start, not a pattern: `_` is a character like any other.
    assert repeating.find(database, "5f0c_") is None


def test_nothing_kept_is_nothing_found_and_nothing_made(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"

    assert repeating.recent(database) == []
    assert repeating.find(database, "5f0c") is None
    assert repeating.family(database, ORIGINAL) == []
    assert not database.exists()


# --- side by side ----------------------------------------------------------------


def test_the_comparison_says_whether_each_repeat_saw_the_same_files(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database, "again-1", later=-1, model="haiku", repeat_of=ORIGINAL)
    kept(database)
    kept(database, "again-2", later=2, model="opus", head="9d9d9d9d", repeat_of=ORIGINAL)
    kept(database, "again-3", later=3, model="haiku", content=None, repeat_of=ORIGINAL)

    runs = repeating.family(database, ORIGINAL)
    lines = repeating.compared(database, runs)

    # The original first, whenever its repeats ran.
    assert [run.id for run in runs] == [ORIGINAL, "again-1", "again-2", "again-3"]
    assert lines[0].startswith("proof on alpha-engine#386 · step developed · ")
    assert [line.split()[-1] for line in lines[2:]] == ["same", "-", "yes", "no", "?"]


def test_the_comparison_joins_what_each_run_used(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database)
    kept(database, "again-1", later=1, model="haiku", repeat_of=ORIGINAL)
    usage.record(
        database,
        [
            usage.Turn(
                "claude-code", ORIGINAL, "claude-sonnet-5", "inspect proof", "alpha-engine",
                12, 3400, 21000, 164000, 0.23,
            ),
            usage.Turn(
                "claude-code", "again-1", "claude-haiku-4-5", "inspect proof · repeat",
                "alpha-engine", 10, 900, 20000, 0, 0.02,
            ),
        ],
    )  # fmt: skip

    lines = repeating.compared(database, repeating.family(database, ORIGINAL))

    original, again = lines[3], lines[4]
    assert original.split()[8:11] == ["185,012", "3,400", "0.23"]
    assert again.split()[9:12] == ["20,010", "900", "0.02"]


def test_the_runs_are_written_out_for_whoever_judges_them(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database)
    kept(database, "again-1", later=1, model="zai/glm-4.6", answer=None, repeat_of=ORIGINAL)
    into = tmp_path / "compared"

    repeating.write(into, repeating.family(database, ORIGINAL), ["the table"])

    assert sorted(path.name for path in into.iterdir()) == [
        "00-original-sonnet-5f0c9a2e.md",
        "01-repeat-1-zai_glm-4.6-again-1.md",
        "input.md",
        "runs.md",
    ]
    assert (into / "input.md").read_text() == INPUT
    assert (into / "00-original-sonnet-5f0c9a2e.md").read_text() == "proof · no finding"
    assert (into / "01-repeat-1-zai_glm-4.6-again-1.md").read_text() == (
        "(no answer: the model did not answer)"
    )


# --- the command -----------------------------------------------------------------


class Runner:
    """A runtime's runner, answering every turn the same and remembering them."""

    id = "claude-code"

    def __init__(self) -> None:
        self.asked: list[dict] = []

    async def ask(self, text: str, **how) -> str | None:
        self.asked.append({"text": text, **how})
        return "proof · status: candidate"


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A machine with one project, one kept run, and a runner that answers."""
    database = tmp_path / "halyard.db"
    kept(database)
    runner = Runner()
    project = Project(
        name="alpha-engine", path=tmp_path, seats=[], label_findings=("status: candidate",)
    )
    monkeypatch.setattr(inspect_cli, "_settings", lambda: SimpleNamespace(db_path=database))
    monkeypatch.setattr(inspect_cli, "projects", lambda: [project])
    monkeypatch.setattr(inspect_cli, "_one_shot", lambda settings: runner)
    monkeypatch.setattr(inspect_cli.frame, "context", lambda path, name: NOW)
    return SimpleNamespace(database=database, runner=runner, project=project)


def test_a_repeat_runs_in_the_project_and_is_kept_beside_the_original(
    here: SimpleNamespace, capsys
) -> None:
    code = inspect_cli.main(["repeat", "5f0c9", "--model", "haiku", "--times=2"])

    assert code == 0
    assert len(here.runner.asked) == 2
    first = here.runner.asked[0]
    assert (first["text"], first["model"], first["cwd"]) == (INPUT, "haiku", here.project.path)
    # Its tokens are told apart from the inspections the work runs.
    assert (first["purpose"], first["project"]) == ("inspect proof · repeat", "alpha-engine")
    assert repeating.repeats(here.database, [ORIGINAL]) == {ORIGINAL: 2}
    again = repeating.find(here.database, first["session_id"])
    assert again is not None
    assert (again.runtime, again.finding) == ("claude-code", "status: candidate")
    out = capsys.readouterr().out
    # Named as its cards name it, so a card can be told for this run's.
    session = first["session_id"]
    assert f"session {session[:4]}…{session[-4:]}" in out
    assert "halyard inspect compare 5f0c9a2e" in out


def test_compare_on_a_repeat_compares_its_original_and_writes_the_runs(
    here: SimpleNamespace, capsys
) -> None:
    kept(here.database, "again-1", later=1, model="haiku", repeat_of=ORIGINAL)

    code = inspect_cli.main(["compare", "again-1"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.index("original") < out.index("repeat 1")
    # Beside the database, under the project — where the service keeps a
    # project's agent prose, and git does not look.
    into = here.database.parent / "projects" / "alpha-engine" / "inspections" / ORIGINAL
    assert (into / "input.md").read_text() == INPUT
    assert str(into) in out


def test_recent_lists_the_runs_with_their_ids(here: SimpleNamespace, capsys) -> None:
    kept(here.database, "again-1", later=1, model="haiku", repeat_of=ORIGINAL)

    assert inspect_cli.main(["recent"]) == 0

    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith("5f0c9a2e  ")
    assert "proof · developed" in line
    assert line.endswith("repeated 1x")


def test_recent_with_nothing_kept_says_how_runs_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        inspect_cli, "_settings", lambda: SimpleNamespace(db_path=tmp_path / "halyard.db")
    )

    assert inspect_cli.main(["recent"]) == 0

    assert "HALYARD_KEEP_INSPECTIONS" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "said"),
    [
        ([], "usage: halyard inspect"),
        (["compare"], "usage: halyard inspect"),
        (["recent", "a week"], "not a number of runs"),
        (["repeat", "5f0c9", "--effort"], "--effort needs a value"),
        (["repeat", "5f0c9", "--modle", "haiku"], "there is no --modle"),
        (["repeat", "5f0c9", "--model"], "--model needs a value"),
        (["repeat", "5f0c9", "--model", "haiku", "--times", "0"], "a number of runs"),
        (["repeat", "0000", "--model", "haiku"], "no run kept"),
    ],
)
def test_what_it_cannot_do_is_refused_before_anything_runs(
    here: SimpleNamespace, capsys, args: list[str], said: str
) -> None:
    assert inspect_cli.main(args) == 2

    assert said in capsys.readouterr().err
    assert here.runner.asked == []


def test_a_repeat_needs_the_project_it_ran_in(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(inspect_cli, "projects", lambda: [])

    assert inspect_cli.main(["repeat", "5f0c9", "--model", "haiku"]) == 2

    assert "alpha-engine has no `path:`" in capsys.readouterr().err
    assert here.runner.asked == []


@pytest.mark.parametrize(
    ("told", "asked"),
    [
        ([], ("sonnet", "max")),
        (["--model", "opus"], ("opus", "max")),
        (["--effort", "High"], ("sonnet", "high")),
        (["--effort", "default"], ("sonnet", None)),
    ],
)
def test_a_repeat_changes_only_what_it_is_told(
    here: SimpleNamespace, told: list[str], asked: tuple[str, str | None]
) -> None:
    """One thing at a time: another model at the same effort, or the same model
    thinking harder — and `default` hands the effort back to the runtime."""
    kept(here.database, effort="max")

    assert inspect_cli.main(["repeat", "5f0c9", *told]) == 0

    [turn] = here.runner.asked
    assert (turn["model"], turn["effort"]) == asked
    again = repeating.find(here.database, turn["session_id"])
    assert again is not None
    assert (again.model, again.effort) == asked


def test_the_comparison_says_how_hard_each_run_thought(tmp_path: Path) -> None:
    database = tmp_path / "halyard.db"
    kept(database)
    kept(database, "again-1", later=1, effort="max", repeat_of=ORIGINAL)

    lines = repeating.compared(database, repeating.family(database, ORIGINAL))

    header, original, again = lines[2], lines[3], lines[4]
    assert header.split()[2] == "effort"
    # `repeat 1` is two words where `original` is one.
    assert (original.split()[3], again.split()[4]) == ("default", "max")


def test_runs_kept_in_the_first_table_are_read_and_kept_beside(tmp_path: Path) -> None:
    """The table as it was made on 2026-09-23 — a `handoff` column, no `effort`:
    a machine that kept runs then has them read, their transition kept, and new
    ones written, without a step of its own."""
    import contextlib
    import sqlite3

    database = tmp_path / "halyard.db"
    before = inspections.record._SCHEMA.replace("    effort       TEXT,\n", "").replace(
        "    transition   TEXT,\n", "    handoff      TEXT,\n"
    )
    assert "effort" not in before and "transition" not in before, (
        "the table as it was, or this proves nothing"
    )
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(before)
        db.execute(
            "INSERT INTO inspection_runs (id, at, project, inspection, file, file_version, "
            "handoff, model, context, note, input, outcome, why, took) VALUES ('old-run', "
            "'2026-09-23T18:00:00+00:00', 'alpha-engine', 'proof', 'NOTES/proof.md', "
            "'3e8c847', 'close', 'sonnet', '', '', 'the input', 'answered', '', 41.5)"
        )
        db.commit()

    [old] = repeating.recent(database)
    kept(database, "new-run", later=1, effort="max")

    assert (old.id, old.transition, old.effort) == ("old-run", "close", None)
    assert {run.id: run.effort for run in repeating.recent(database)} == {
        "old-run": None,
        "new-run": "max",
    }


def test_inspect_is_a_command_of_its_own(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import halyard.__main__ as entry

    started = []
    monkeypatch.setattr(entry, "serve", lambda: started.append(True))
    monkeypatch.setattr(sys, "argv", ["halyard", "inspect"])

    with pytest.raises(SystemExit) as exit_info:
        entry.main()

    assert exit_info.value.code == 2
    assert started == []
    assert "usage: halyard inspect" in capsys.readouterr().err
