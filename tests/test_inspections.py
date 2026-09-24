"""Tests for `halyard.inspections` — one inspection, run over a reply, through `Asker`."""

from __future__ import annotations

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


class Labelling:
    """A `Labeller` that keeps what it was asked to put on the task."""

    def __init__(self, *, fails: bool = False) -> None:
        self.put: list[str] = []
        self.fails = fails

    async def label(self, label: str) -> None:
        if self.fails:
            raise RuntimeError("the tracker is down")
        self.put.append(label)


async def checked(
    tmp_path: Path, says: str | None, labeller: Labelling, findings=("status: candidate",)
) -> inspections.Answer:
    (tmp_path / "proof.md").write_text("# proof\n")
    return await inspections.run(
        "proof",
        Path("proof.md"),
        project=tmp_path,
        context=[],
        note="",
        reply="42 passed",
        asker=Asking(says=says),
        model="sonnet",
        timeout=5,
        findings=findings,
        labeller=labeller,
    )


async def ran(
    tmp_path: Path, asker: Asking, check: str = "proof.md", *, handoff: str = ""
) -> inspections.Answer:
    return await inspections.run(
        "proof",
        Path(check),
        project=tmp_path,
        context=["Work item: alpha-engine#355"],
        note="delivery",
        reply="42 passed",
        asker=asker,
        model="sonnet",
        timeout=5,
        handoff=handoff,
    )


def test_the_prompt_says_where_the_check_stands_and_what_it_may_do() -> None:
    """In the project, reading but never editing; running only what the check's
    own text names — measured: told only that it could run commands, a claims
    check ran the suite test by test, wrote probes of its own and made scratch
    copies — and reading for the check rather than through everything the
    project's own instructions name."""
    asked = inspections.prompt(
        "# proof", context=["Project: alpha-engine"], note="delivery", text="42 passed"
    )

    assert asked.startswith("# proof")
    assert "runs in the project's own directory" in asked
    assert "cannot edit anything" in asked
    assert "only the commands this check's text tells it to" in asked
    assert "nothing of its own" in asked
    assert "rather than a reading list" in asked
    assert "Envelope:\n- Project: alpha-engine\n- Said by whoever asked: delivery" in asked
    assert "delivery" in asked
    assert asked.endswith("42 passed")


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
    that code is — Halyard's own directory is another repository, and a check
    started there found nothing it could compare. It may look and run, and edit
    nothing."""
    (tmp_path / "proof.md").write_text("# proof\n")
    asker = Asking()

    await ran(tmp_path, asker)

    assert asker.how == [{"cwd": tmp_path, "name": "proof", "edits": False}]


async def test_a_check_run_for_a_handoff_goes_by_both_names(tmp_path: Path) -> None:
    """Whatever it asks a person for says which check and which handoff."""
    (tmp_path / "proof.md").write_text("# proof\n")
    asker = Asking()

    await ran(tmp_path, asker, handoff="discover_completed")

    assert asker.how[0]["name"] == "proof · handoff discover_completed"


async def test_a_model_that_does_not_answer_is_said_not_skipped(tmp_path: Path) -> None:
    """A missing line reads exactly like a clean one."""
    (tmp_path / "proof.md").write_text("# proof\n")

    answer = await ran(tmp_path, Asking(says=None))

    assert not answer.measured
    assert answer.why == "the model did not answer"


async def test_a_check_that_cannot_be_read_never_asks(tmp_path: Path) -> None:
    asker = Asking()

    answer = await ran(tmp_path, asker, check="gone.md")

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


def test_a_finding_is_the_projects_own_words_whatever_their_case() -> None:
    """Whatever the project's check files have the model write, matched as
    written give or take case; no phrases means nothing is ever a finding."""
    findings = ("status: candidate", "status: evidence missing")

    assert (
        inspections.finding("proof · #355 · Status: Candidate\n- one", findings)
        == "status: candidate"
    )
    assert inspections.finding("proof · #355 · status: no finding", findings) is None
    assert inspections.finding("proof · #355 · status: candidate", ()) is None


async def test_a_check_that_finds_something_labels_the_task(tmp_path: Path) -> None:
    """Decided by the check, from the project's own words — so a check run by
    hand and one run by a handoff label alike."""
    labeller = Labelling()

    await checked(tmp_path, "proof · status: candidate", labeller)

    assert labeller.put == ["halyard:proof"]


async def test_nothing_is_labelled_without_a_finding(tmp_path: Path) -> None:
    """Not for a clean answer, not for one that never came, and not for a
    project that has not said what a finding looks like."""
    labeller = Labelling()

    await checked(tmp_path, "proof · status: no finding", labeller)
    await checked(tmp_path, None, labeller)
    await checked(tmp_path, "proof · status: candidate", labeller, findings=())

    assert labeller.put == []


async def test_a_label_that_cannot_be_written_costs_the_label_not_the_answer(
    tmp_path: Path,
) -> None:
    answer = await checked(tmp_path, "proof · status: candidate", Labelling(fails=True))

    assert answer.measured
    assert answer.text == "proof · status: candidate"


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


async def kept_run(
    tmp_path: Path, asker, keeper: Keeping, *, says_finding: bool = False, effort=None
):
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
        handoff="close",
        findings=("status: candidate",) if says_finding else (),
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

    await kept_run(tmp_path, asker, keeper, says_finding=True)

    [kept] = keeper.kept
    [(text, _)] = asker.asked
    assert kept.asked == text
    assert kept.session == asker.sessions[0]
    assert (kept.name, kept.handoff, kept.model) == ("proof", "close", "sonnet")
    assert kept.answer == "proof · status: candidate"
    assert kept.finding == "status: candidate"
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
        handoff="close",
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
