"""Tests for `halyard.inspections` — one inspection, run over a reply, through `Asker`."""

from __future__ import annotations

import re
from pathlib import Path

from halyard import inspections


class Asking:
    """An `Asker` that answers from a script and remembers what it was asked."""

    def __init__(self, says: str | None = "proof · no finding") -> None:
        self.says = says
        self.asked: list[tuple[str, str | None]] = []
        #: Where each turn was to run, what it went by, and whether it could edit.
        self.how: list[dict] = []
        #: The id each turn was asked to run under.
        self.sessions: list[str | None] = []
        #: How hard each turn was asked to think.
        self.efforts: list[str | None] = []

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        name: str | None = None,
        edits: bool = True,
        session_id: str | None = None,
        effort: str | None = None,
    ) -> str | None:
        self.asked.append((text, model))
        self.how.append({"cwd": cwd, "name": name, "edits": edits})
        self.sessions.append(session_id)
        self.efforts.append(effort)
        if self.says is None:
            raise RuntimeError("no model today")
        return self.says


async def ran(
    tmp_path: Path, asker: Asking, inspection: str = "proof.md", *, transition: str = ""
) -> inspections.Answer:
    return await inspections.run(
        "proof",
        Path(inspection),
        project=tmp_path,
        context=["Work item: alpha-engine#355"],
        note="delivery",
        reply="42 passed",
        asker=asker,
        model="sonnet",
        timeout=5,
        transition=transition,
    )


def test_the_prompt_says_where_the_check_stands_and_what_it_may_do() -> None:
    """The project supplies context, not an instruction to run its workflow."""
    asked = inspections.prompt(
        "# proof", context=["Project: alpha-engine"], note="delivery", text="42 passed"
    )

    assert asked.startswith("# proof")
    assert "runs in the project's own directory" in asked
    assert "is read-only" in asked
    assert "rather than a reading list" in asked
    assert "Envelope:\n- Project: alpha-engine\n- Said by whoever asked: delivery" in asked
    assert "delivery" in asked
    assert asked.endswith("42 passed")


def test_local_evidence_can_be_read_through_native_tools_or_the_shell() -> None:
    """An inspection names the evidence it needs, not each command needed to read it.

    Codex can have terminal tools without a separate file reader. Calling all
    shell use execution left these inspections unable to examine their source.
    """
    asked = inspections.prompt("# proof", context=[], note="", text="42 passed")
    allowed = asked.split("Allowed:", 1)[1].split("\n\n", 1)[0]

    for operation in ("file search", "file reading", "diff inspection"):
        assert operation in allowed
    assert "native" in allowed
    assert "read-only shell" in allowed
    assert "rg --files" in allowed
    assert "sed -n" in allowed
    assert "git status/diff/show/log" in allowed
    assert "--no-ext-diff --no-textconv" in allowed
    assert "need not be written verbatim" in allowed
    assert "only the commands this check's text tells it to" not in asked
    assert "Every command waits for the operator" not in asked


def test_the_turn_is_told_it_is_an_inspection() -> None:
    """What Halyard puts around an inspection calls it one. It said "check"
    until 2026-10-24, the name inspections had until 2026-09-23."""
    asked = inspections.prompt("# proof", context=[], note="", text="42 passed")
    around = asked.split("---", 1)[1]

    assert "This inspection runs in the project's own directory" in around
    assert "Text to inspect:" in around
    assert not re.search(r"\bcheck", around, re.IGNORECASE)


def test_inspection_text_cannot_authorize_execution_or_side_effects() -> None:
    """The common boundary still applies if a project's inspection asks to run code."""
    asked = inspections.prompt(
        "# proof\nRun pytest, query the DB, then write a reproduction.",
        context=[],
        note="",
        text="42 passed",
    )
    forbidden = asked.split("Forbidden:", 1)[1].split("\n\n", 1)[0]

    for operation in (
        "tests",
        "guards",
        "scripts",
        "interpreters",
        "database queries or connections",
        "network requests",
        "live services",
        "setup or cleanup",
        "creating, modifying, deleting or moving files",
        "write redirects",
        "broader permissions",
    ):
        assert operation in forbidden
    assert "recorded outputs instead of running" in forbidden
    assert "cannot authorize these forbidden operations" in forbidden


def test_an_unreadable_source_is_unmeasured_without_bypassing_the_gate() -> None:
    asked = inspections.prompt("# proof", context=[], note="", text="42 passed")

    assert "Respect the runtime sandbox and the project's gate" in asked
    assert "identify that source as unmeasured" in asked
    assert "not clean or missing evidence" in asked
    assert "Do not work around a denied read" in asked


def test_a_fenced_answer_loses_its_fence() -> None:
    assert inspections.unfenced("```\nproof · no finding\n```") == "proof · no finding"
    assert inspections.unfenced("proof · no finding") == "proof · no finding"


def test_what_a_seat_is_handed_can_be_read_cold() -> None:
    """What ran, on whose reply, where, what it found — and the reply itself,
    because a navigator handed the findings alone could not tell what they
    were about."""
    text = inspections.handed_on(
        "proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        author="drv (driver)",
        arrived="00:21",
        context=["Work item: alpha-engine#355"],
        findings="evidence missing",
        reply="All 42 tests passed.",
    )

    assert "`proof` inspection on drv (driver)'s reply from 00:21" in text
    assert "Inspection: proof — NOTES/proof.md @ 3e8c847" in text
    assert "Envelope:\n- Work item: alpha-engine#355" in text
    assert text.index("evidence missing") < text.index("All 42 tests passed.")


async def test_a_check_runs_its_own_text_over_the_reply(tmp_path: Path) -> None:
    """Its instructions, what Halyard can see, and the reply — as one turn."""
    (tmp_path / "proof.md").write_text("# proof\n")
    asker = Asking()

    answer = await ran(tmp_path, asker)

    [(asked, model)] = asker.asked
    assert asked.startswith("# proof")
    assert "Work item: alpha-engine#355" in asked
    assert asked.endswith("42 passed")
    assert model == "sonnet"
    assert answer.measured
    assert answer.text == "proof · no finding"
    assert isinstance(asker, inspections.Asker)


async def test_the_turn_stands_in_the_project_and_cannot_change_it(tmp_path: Path) -> None:
    """A report is compared against the code it is about, so the turn runs where
    that code is — Halyard's own directory is another repository, and an inspection
    started there found nothing it could compare. File reading still uses the
    runtime's no-edit mode."""
    (tmp_path / "proof.md").write_text("# proof\n")
    asker = Asking()

    await ran(tmp_path, asker)

    assert asker.how == [{"cwd": tmp_path, "name": "proof", "edits": False}]


async def test_an_inspection_run_for_a_transition_goes_by_both_names(tmp_path: Path) -> None:
    """Whatever it asks a person for says which inspection and which transition."""
    (tmp_path / "proof.md").write_text("# proof\n")
    asker = Asking()

    await ran(tmp_path, asker, transition="discover_completed")

    assert asker.how[0]["name"] == "proof · transition discover_completed"


async def test_a_model_that_does_not_answer_is_said_not_skipped(tmp_path: Path) -> None:
    """A missing line reads exactly like a clean one."""
    (tmp_path / "proof.md").write_text("# proof\n")

    answer = await ran(tmp_path, Asking(says=None))

    assert not answer.measured
    assert answer.why == "the model did not answer"


async def test_a_check_that_cannot_be_read_never_asks(tmp_path: Path) -> None:
    asker = Asking()

    answer = await ran(tmp_path, asker, inspection="gone.md")

    assert asker.asked == []
    assert "could not read" in answer.why


async def test_a_check_somebody_stopped_says_so(tmp_path: Path) -> None:
    """Unmeasured, with who stopped it — never read as a model that went quiet."""
    (tmp_path / "proof.md").write_text("# proof\n")

    class Stopping(Asking):
        async def ask(self, text: str, **_) -> str | None:
            raise inspections.StoppedError("stopped by tg:4242")

    answer = await ran(tmp_path, Stopping())

    assert not answer.measured
    assert answer.why == "stopped by tg:4242"


# --- every run, kept whole --------------------------------------------------


class Keeping:
    """A `Keeper` that keeps what it was given, or fails when told to."""

    def __init__(self, *, fails: bool = False) -> None:
        self.kept: list[inspections.Kept] = []
        self.fails = fails

    async def keep(self, kept: inspections.Kept) -> None:
        if self.fails:
            raise RuntimeError("the disk is full")
        self.kept.append(kept)


async def kept_run(tmp_path: Path, asker, keeper: Keeping, *, effort=None):
    (tmp_path / "proof.md").write_text("# proof\n")
    return await inspections.run(
        "proof",
        Path("proof.md"),
        project=tmp_path,
        context=["Work item: alpha-engine#386", "HEAD: 2cdcab9c", "Content: 5df40c87 (write-tree)"],
        note="delivery",
        reply="42 passed",
        asker=asker,
        model="sonnet",
        effort=effort,
        timeout=5,
        transition="close",
        keeper=keeper,
    )


async def test_the_effort_asked_for_is_the_one_kept(tmp_path: Path) -> None:
    """How hard a model thought is half of what a run is compared by."""
    asker, keeper = Asking(), Keeping()

    await kept_run(tmp_path, asker, keeper, effort="max")

    assert asker.efforts == ["max"]
    [kept] = keeper.kept
    assert (kept.model, kept.effort) == ("sonnet", "max")


async def test_every_run_is_kept_whole_under_the_id_it_ran_under(tmp_path: Path) -> None:
    """Word for word what the model was given, what it said, and the id its
    tokens are recorded by — which is what makes a run comparable later."""
    asker, keeper = Asking(says="proof · status: candidate"), Keeping()

    await kept_run(tmp_path, asker, keeper)

    [kept] = keeper.kept
    [(text, _)] = asker.asked
    assert kept.asked == text
    assert kept.session == asker.sessions[0]
    assert (kept.name, kept.transition, kept.model) == ("proof", "close", "sonnet")
    assert kept.answer == "proof · status: candidate"
    # Findings no longer label tasks, so none is read out of the answer.
    assert kept.finding is None
    assert kept.context[1] == "HEAD: 2cdcab9c"
    assert "42 passed" in kept.asked


async def test_a_run_with_no_answer_is_kept_as_well(tmp_path: Path) -> None:
    keeper = Keeping()

    await kept_run(tmp_path, Asking(says=None), keeper)

    [kept] = keeper.kept
    assert kept.answer is None
    assert kept.why == "the model did not answer"


async def test_a_stopped_run_is_kept_with_who_stopped_it(tmp_path: Path) -> None:
    class Stopping(Asking):
        async def ask(self, text: str, **_) -> str | None:
            raise inspections.StoppedError("stopped by tg:4242")

    keeper = Keeping()

    await kept_run(tmp_path, Stopping(), keeper)

    [kept] = keeper.kept
    assert (kept.answer, kept.why) == (None, "stopped by tg:4242")


async def test_an_inspection_that_never_reached_a_model_is_not_kept(tmp_path: Path) -> None:
    keeper = Keeping()

    await inspections.run(
        "proof",
        Path("gone.md"),
        project=tmp_path,
        context=[],
        note="",
        reply="42 passed",
        asker=Asking(),
        model="sonnet",
        timeout=5,
        keeper=keeper,
    )

    assert keeper.kept == []


async def test_a_record_that_cannot_be_kept_costs_the_record_only(tmp_path: Path) -> None:
    answer = await kept_run(tmp_path, Asking(), Keeping(fails=True))

    assert answer.measured


def test_a_kept_run_joins_the_tokens_it_used(tmp_path: Path) -> None:
    """One table for what was said, one for what it cost, one id between them."""
    import sqlite3
    from datetime import UTC, datetime

    from halyard.core import usage

    database = tmp_path / "halyard.db"
    kept = inspections.Kept(
        session="sess-1",
        at=datetime(2026, 9, 23, 18, 0, tzinfo=UTC),
        name="proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        transition="close",
        model="sonnet",
        asked="# proof\n\nText to check:\n\n42 passed",
        context=("Work item: alpha-engine#386", "HEAD: 2cdcab9c", "Content: 5df40c87 (write-tree)"),
        note="",
        answer="proof · no finding",
        why="",
        finding=None,
        took=41.5,
    )
    inspections.record.keep(
        database,
        kept,
        project="alpha-engine",
        work="alpha-engine#386",
        runtime="claude-code",
        workflow_run="alpha-engine#386 2026-09-23T17:00:00+00:00",
        step="developed",
        phase=2,
        round=1,
    )
    usage.record(
        database,
        [
            usage.Turn(
                "claude-code",
                "sess-1",
                "claude-sonnet-5",
                "inspect proof",
                "alpha-engine",
                12,
                3400,
                21000,
                164000,
                0.23,
            )
        ],
    )

    with sqlite3.connect(database) as db:
        [row] = db.execute(
            "SELECT r.inspection, r.head, r.content, r.outcome, r.step, r.phase, r.round, "
            "r.experimental, u.output_tokens "
            "FROM inspection_runs r JOIN turn_usage u ON u.session_id = r.id"
        ).fetchall()
    assert row == ("proof", "2cdcab9c", "5df40c87", "answered", "developed", 2, 1, 0, 3400)


# --- reads that did not run ------------------------------------------------------


def test_the_turn_is_told_to_wait_for_a_command_and_not_to_answer_past_it() -> None:
    asked = inspections.prompt("# proof", context=[], note="", text="42 passed")

    assert (
        "A command may wait minutes for the operator's approval: wait for it to "
        "return before answering, and never answer as if you read what a command "
        "that has not returned would have shown."
    ) in asked


def test_reads_that_did_not_run_are_said_in_halyard_s_words() -> None:
    one = (inspections.NotRun("cat /tmp/x.log", "denied by tg:4242"),)
    two = (*one, inspections.NotRun("sed -n 1,5p a.py", "expired before anybody answered"))

    assert inspections.not_run_block(()) == ""
    assert inspections.not_run_block(one) == (
        "Halyard: 1 read this inspection asked for did not run — `cat /tmp/x.log` "
        "(denied by tg:4242). Anything that rests on it is not measured."
    )
    assert inspections.not_run_block(two) == (
        "Halyard: 2 reads this inspection asked for did not run — `cat /tmp/x.log` "
        "(denied by tg:4242); `sed -n 1,5p a.py` (expired before anybody answered). "
        "Anything that rests on them is not measured."
    )


class Gating(Asking):
    """An `Asker` that can say what its turn read, and what became of each read."""

    def __init__(self, *reads: inspections.Read, says: str | None = "proof · no finding"):
        super().__init__(says)
        self.reads = reads

    def trace(self, session_id: str) -> inspections.Trace:
        reads = self.reads if session_id == self.sessions[-1] else ()
        return inspections.Trace(reads, reported=True)


RAN = inspections.Read("Read", "src/app.py", inspections.Outcome.RAN)


async def test_an_answer_carries_the_reads_that_did_not_run_and_so_does_its_record(
    tmp_path: Path,
) -> None:
    read = inspections.NotRun(
        "cat /tmp/x.log", "still waiting for approval when the inspection ended"
    )
    ended = inspections.Read("Bash", read.command, inspections.Outcome.ENDED, read.why)
    asker, keeper = Gating(RAN, ended), Keeping()

    answer = await kept_run(tmp_path, asker, keeper)

    assert isinstance(asker, inspections.Gated)
    assert answer.text == "proof · no finding"
    assert answer.not_run == (read,)
    [kept] = keeper.kept
    assert kept.answer == "proof · no finding", "the model's answer is kept as it came"
    assert kept.not_run == inspections.not_run_block((read,))


async def test_an_unanswered_inspection_still_carries_its_reads_that_did_not_run(
    tmp_path: Path,
) -> None:
    read = inspections.NotRun("cat a", "denied by tg:4242")
    denied = inspections.Read("Bash", "cat a", inspections.Outcome.DENIED, read.why)

    answer = await kept_run(tmp_path, Gating(denied, says=None), Keeping())

    assert not answer.measured
    assert answer.not_run == (read,)


async def test_an_asker_that_cannot_tell_adds_nothing(tmp_path: Path) -> None:
    keeper = Keeping()

    answer = await kept_run(tmp_path, Asking(), keeper)

    assert answer.not_run == ()
    assert keeper.kept[0].not_run == ""


def test_what_halyard_said_under_an_answer_is_kept_in_a_column_of_its_own(
    tmp_path: Path,
) -> None:
    """Beside the answer, not in it: the model's words stay comparable. A table
    made before the column gets it, empty for the rows it had."""
    import sqlite3
    from datetime import UTC, datetime

    database = tmp_path / "halyard.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE inspection_runs (id TEXT PRIMARY KEY, at TEXT NOT NULL, "
            "project TEXT NOT NULL, work TEXT, inspection TEXT NOT NULL, file TEXT NOT NULL, "
            "file_version TEXT NOT NULL, transition TEXT, workflow_run TEXT, step TEXT, "
            "phase INTEGER, round INTEGER, runtime TEXT, model TEXT NOT NULL, effort TEXT, "
            "head TEXT, content TEXT, context TEXT NOT NULL, note TEXT NOT NULL, "
            "input TEXT NOT NULL, answer TEXT, outcome TEXT NOT NULL, why TEXT NOT NULL, "
            "finding TEXT, took REAL NOT NULL, experimental INTEGER NOT NULL DEFAULT 0, "
            "repeat_of TEXT)"
        )
        db.execute(
            "INSERT INTO inspection_runs (id, at, project, inspection, file, file_version, "
            "model, context, note, input, outcome, why, took) VALUES ('old', '2026-10-01', "
            "'alpha-engine', 'proof', 'NOTES/proof.md', 'abc', 'sonnet', '', '', '', "
            "'answered', '', 1.0)"
        )
    said = "Halyard: 1 read this inspection asked for did not run — `cat a` (denied)."
    kept = inspections.Kept(
        session="sess-2",
        at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        name="proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        transition="",
        model="sonnet",
        asked="# proof",
        context=(),
        note="",
        answer="proof · no finding",
        why="",
        finding=None,
        took=12.0,
        not_run=said,
    )

    inspections.record.keep(database, kept, project="alpha-engine", work=None, runtime="codex")

    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id, answer, not_run FROM inspection_runs ORDER BY id").fetchall()
    assert rows == [("old", None, None), ("sess-2", "proof · no finding", said)]


async def test_a_run_s_status_comes_from_what_it_read_and_is_kept_with_its_trace(
    tmp_path: Path,
) -> None:
    ended = inspections.Read("Bash", "cat /tmp/x.log", inspections.Outcome.ENDED, "still waiting")

    complete = Keeping()
    answer = await kept_run(tmp_path, Gating(RAN), complete)
    assert answer.status is inspections.Status.COMPLETE
    assert (complete.kept[0].status, complete.kept[0].trace) == (
        "complete",
        inspections.Trace((RAN,), reported=True),
    )

    incomplete = Keeping()
    answer = await kept_run(tmp_path, Gating(RAN, ended), incomplete)
    assert answer.status is inspections.Status.INCOMPLETE
    assert answer.text == "proof · no finding", "marked, and the answer stands"
    assert incomplete.kept[0].status == "incomplete"

    unmeasured = Keeping()
    answer = await kept_run(tmp_path, Gating(RAN, ended, says=None), unmeasured)
    assert answer.status is inspections.Status.UNMEASURED
    assert unmeasured.kept[0].status == "unmeasured"
    assert unmeasured.kept[0].trace == inspections.Trace((RAN, ended), reported=True)


def test_a_table_made_before_statuses_gets_the_columns(tmp_path: Path) -> None:
    """Added the way `effort` and `not_run` were: empty for the rows it had."""
    import json
    import sqlite3
    from datetime import UTC, datetime

    database = tmp_path / "halyard.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE inspection_runs (id TEXT PRIMARY KEY, at TEXT NOT NULL, "
            "project TEXT NOT NULL, work TEXT, inspection TEXT NOT NULL, file TEXT NOT NULL, "
            "file_version TEXT NOT NULL, transition TEXT, workflow_run TEXT, step TEXT, "
            "phase INTEGER, round INTEGER, runtime TEXT, model TEXT NOT NULL, effort TEXT, "
            "head TEXT, content TEXT, context TEXT NOT NULL, note TEXT NOT NULL, "
            "input TEXT NOT NULL, answer TEXT, outcome TEXT NOT NULL, why TEXT NOT NULL, "
            "finding TEXT, not_run TEXT, took REAL NOT NULL, "
            "experimental INTEGER NOT NULL DEFAULT 0, repeat_of TEXT)"
        )
        db.execute(
            "INSERT INTO inspection_runs (id, at, project, inspection, file, file_version, "
            "model, context, note, input, outcome, why, took) VALUES ('old', '2026-10-01', "
            "'alpha-engine', 'proof', 'NOTES/proof.md', 'abc', 'sonnet', '', '', '', "
            "'answered', '', 1.0)"
        )
    kept = inspections.Kept(
        session="sess-3",
        at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        name="proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        transition="",
        model="sonnet",
        asked="# proof",
        context=(),
        note="",
        answer="proof · no finding",
        why="",
        finding=None,
        took=12.0,
        status="complete",
        trace=inspections.Trace((RAN,), reported=True),
    )

    inspections.record.keep(database, kept, project="alpha-engine", work=None, runtime="codex")

    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id, status, trace FROM inspection_runs ORDER BY id").fetchall()
    assert rows[0] == ("old", None, None)
    assert rows[1][:2] == ("sess-3", "complete")
    assert json.loads(rows[1][2]) == {
        "reported": True,
        "reads": [{"tool": "Read", "what": "src/app.py", "outcome": "ran", "detail": ""}],
    }
