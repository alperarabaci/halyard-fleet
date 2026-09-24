"""Tests for `halyard.handoffs` — a reply handed on, its checks run first."""

from __future__ import annotations

from pathlib import Path

from halyard import handoffs
from halyard.commands import Command, Result
from halyard.core.config_file import Handoff, ModelChoice


class Asking:
    """An `Asker` that answers from a script and remembers what it was asked."""

    def __init__(self, says: str | None = "no finding") -> None:
        self.says = says
        self.asked: list[str] = []
        #: What each turn went by, as a card for one of its commands would say.
        self.names: list[str | None] = []
        #: The model and effort each turn was asked for, by what it went by.
        self.models: dict[str | None, tuple[str | None, str | None]] = {}

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
        self.asked.append(text)
        self.names.append(name)
        self.models[name] = (model, effort)
        return self.says


class Delivered:
    """A `Delivery` that keeps what reached each seat."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def to_seat(self, label: str, text: str) -> None:
        self.sent.append((label, text))


def a_project(tmp_path: Path) -> Path:
    (tmp_path / "NOTES").mkdir()
    (tmp_path / "NOTES" / "discovery.md").write_text("The message below is the driver's report.")
    (tmp_path / "NOTES" / "proof.md").write_text("# proof")
    (tmp_path / "NOTES" / "claims.md").write_text("# claims")
    return tmp_path


class Running:
    """A `Runner` that answers from a script and keeps what it ran, in order."""

    def __init__(self, **results: Result) -> None:
        self.results = results
        self.ran: list[str] = []

    async def run(self, command: Command) -> Result:
        self.ran.append(command.name)
        return self.results.get(command.name) or Result(
            ok=True, output="collected\n1420 passed", seconds=94.0, exit_code=0
        )


async def hand(tmp_path: Path, handoff: Handoff, *, asker: Asking | None = None, **more):
    delivery = Delivered()
    handed = await handoffs.hand_off(
        handoff,
        project=tmp_path,
        context=["Work item: alpha-engine#355"],
        note="",
        reply="All 42 tests passed.",
        arrived="00:21",
        sender="drv (driver)",
        recipient_label="nav",
        recipient="nav (navigator)",
        project_inspections={"proof": Path("NOTES/proof.md"), "claims": Path("NOTES/claims.md")},
        asker=asker,
        model="sonnet",
        timeout=5,
        delivery=delivery,
        **more,
    )
    return handed, delivery


async def test_the_navigator_gets_the_prompt_the_checks_and_the_report_in_that_order(
    tmp_path: Path,
) -> None:
    """The prompt speaks of "the message below", so the report comes last, and
    the checks' answers arrive with it rather than after it."""
    a_project(tmp_path)
    asker = Asking()
    discovery = Handoff(
        name="discovery",
        prompt=Path("NOTES/discovery.md"),
        inspections=("proof", "claims"),
        to="navigator",
    )

    handed, delivery = await hand(tmp_path, discovery, asker=asker)

    [(label, text)] = delivery.sent
    assert label == "nav"
    assert text.startswith("To nav (navigator), from Halyard — handoff: discovery.")
    order = [
        text.index("The message below"),
        text.index("Inspection proof"),
        text.index("Inspection claims"),
        text.index("All 42 tests passed."),
    ]
    assert order == sorted(order)
    assert "From: drv (driver), reply from 00:21" in text
    assert "Work item: alpha-engine#355" in text
    assert len(asker.asked) == 2
    assert [answer.name for answer in handed.answers] == ["proof", "claims"]
    assert isinstance(delivery, handoffs.Delivery)


def with_followup(tmp_path: Path) -> Handoff:
    """A review with a text of its own for every round after the first."""
    (tmp_path / "NOTES" / "review.md").write_text("Try to break the prompt below.")
    (tmp_path / "NOTES" / "review-followup.md").write_text("Only the earlier BLOCKERs.")
    return Handoff(
        name="review",
        prompt=Path("NOTES/review.md"),
        followup_prompt=Path("NOTES/review-followup.md"),
        to="reviewer",
    )


async def test_the_first_round_sends_the_prompt_and_says_which_round(tmp_path: Path) -> None:
    a_project(tmp_path)

    _, delivery = await hand(tmp_path, with_followup(tmp_path), round_number=1, expected=2)

    [(_, text)] = delivery.sent
    assert "Try to break the prompt below." in text
    assert "Only the earlier BLOCKERs." not in text
    assert "- Round: 1/2" in text


async def test_every_round_after_the_first_sends_the_followup_in_its_place(
    tmp_path: Path,
) -> None:
    """A reviewer asked again is pointed at what it found, not set to review
    everything afresh — which is how alpha-engine#361 went round three times."""
    a_project(tmp_path)

    _, delivery = await hand(tmp_path, with_followup(tmp_path), round_number=3, expected=2)

    [(_, text)] = delivery.sent
    assert "Only the earlier BLOCKERs." in text
    assert "Try to break the prompt below." not in text
    assert "- Round: 3/2" in text
    assert "- Prompt: NOTES/review-followup.md @ " in text


async def test_a_handoff_without_a_followup_sends_its_prompt_every_round(tmp_path: Path) -> None:
    a_project(tmp_path)
    discovery = Handoff(name="discovery", prompt=Path("NOTES/discovery.md"))

    _, delivery = await hand(tmp_path, discovery, round_number=2, expected=2)

    [(_, text)] = delivery.sent
    assert "The message below is the driver's report." in text
    assert "- Round: 2/2" in text


async def test_the_answer_to_the_round_before_comes_ahead_of_the_reply(tmp_path: Path) -> None:
    a_project(tmp_path)
    answered = handoffs.Previous(
        number=1,
        seat="xrev (reviewer)",
        sent="17:07",
        text="BLOCKER: the count is wrong.",
        at="17:09",
    )

    _, delivery = await hand(tmp_path, with_followup(tmp_path), round_number=2, previous=answered)

    [(_, text)] = delivery.sent
    assert "- Previous answer: xrev (reviewer), from 17:09, to round 1" in text
    order = [
        text.index("xrev (reviewer)'s answer to round 1:"),
        text.index("BLOCKER: the count is wrong."),
        text.index("All 42 tests passed."),
    ]
    assert order == sorted(order)


async def test_a_seat_that_has_said_nothing_since_is_said_to_have_not(tmp_path: Path) -> None:
    a_project(tmp_path)
    silent = handoffs.Previous(number=1, seat="xrev (reviewer)", sent="17:07")

    _, delivery = await hand(tmp_path, with_followup(tmp_path), round_number=2, previous=silent)

    [(_, text)] = delivery.sent
    assert "- Previous answer: none — xrev (reviewer) has said nothing since round 1" in text
    assert "answer to round 1:" not in text


async def test_a_handoff_nobody_counts_says_nothing_of_rounds(tmp_path: Path) -> None:
    a_project(tmp_path)

    _, delivery = await hand(tmp_path, with_followup(tmp_path))

    [(_, text)] = delivery.sent
    assert "Round:" not in text
    assert "Try to break the prompt below." in text


async def test_a_check_that_could_not_run_still_goes_marked_unmeasured(tmp_path: Path) -> None:
    """An unmeasured line is not a clean one, and the reader has to see it."""
    a_project(tmp_path)

    handed, delivery = await hand(tmp_path, Handoff(name="discovery", inspections=("proof",)))

    [(_, text)] = delivery.sent
    assert "unmeasured — no runtime here can take a one-shot turn" in text
    assert not handed.answers[0].measured


async def test_a_handoff_can_be_the_reply_alone(tmp_path: Path) -> None:
    """The review going back to the navigator needs nothing in front of it but
    who it is from."""
    handed, delivery = await hand(tmp_path, Handoff(name="back"))

    [(_, text)] = delivery.sent
    assert "Prompt:" not in text
    assert "Check " not in text
    assert text.endswith("All 42 tests passed.")
    assert handed.answers == ()


async def test_a_prompt_that_cannot_be_read_is_said_rather_than_dropped(tmp_path: Path) -> None:
    _, delivery = await hand(tmp_path, Handoff(name="review", prompt=Path("NOTES/gone.md")))

    [(_, text)] = delivery.sent
    assert "NOTES/gone.md @ uncommitted — could not be read" in text


async def test_each_check_a_handoff_runs_goes_by_the_handoffs_name_too(tmp_path: Path) -> None:
    """A command one of them asks to run reaches a person saying which check
    and which handoff it came from."""
    a_project(tmp_path)
    asker = Asking()

    await hand(tmp_path, Handoff(name="discovery", inspections=("proof", "claims")), asker=asker)

    assert sorted(asker.names) == ["claims · handoff discovery", "proof · handoff discovery"]


async def test_an_inspection_that_names_its_own_model_runs_on_it(tmp_path: Path) -> None:
    """One inspection on a stronger model, the rest on the machine's — and what
    it leaves unsaid, the machine's too."""
    a_project(tmp_path)
    asker = Asking()

    await hand(
        tmp_path,
        Handoff(name="close", inspections=("proof", "claims")),
        asker=asker,
        effort="max",
        models={"claims": ModelChoice(model="opus")},
    )

    assert asker.models == {
        "proof · handoff close": ("sonnet", "max"),
        "claims · handoff close": ("opus", "max"),
    }


async def test_the_seat_reads_an_envelope_one_fact_to_a_line(tmp_path: Path) -> None:
    """What Halyard can see, which prompt, who asked and who it is from — as a
    list, not a line of facts run together for a model to pick apart."""
    a_project(tmp_path)

    _, delivery = await hand(tmp_path, Handoff(name="discovery", prompt=Path("NOTES/discovery.md")))

    [(_, text)] = delivery.sent
    assert "Envelope:\n- Work item: alpha-engine#355\n- Prompt: NOTES/discovery.md @ " in text
    assert "\n- From: drv (driver), reply from 00:21" in text


async def test_a_check_in_a_handoff_labels_the_task_as_it_would_by_hand(tmp_path: Path) -> None:
    """No exception for handoffs: the check decides, wherever it runs."""
    a_project(tmp_path)
    put: list[str] = []

    class Labelling:
        async def label(self, label: str) -> None:
            put.append(label)

    await handoffs.hand_off(
        Handoff(name="discovery", inspections=("proof",)),
        project=tmp_path,
        context=[],
        note="",
        reply="All 42 tests passed.",
        arrived="00:21",
        sender="drv (driver)",
        recipient_label="nav",
        recipient="nav (navigator)",
        project_inspections={"proof": Path("NOTES/proof.md")},
        asker=Asking(says="proof · status: candidate"),
        model="sonnet",
        timeout=5,
        delivery=Delivered(),
        findings=("status: candidate",),
        labeller=Labelling(),
    )

    assert put == ["halyard:proof"]


COMMANDS = {"test-fast": "make test-fast", "lint": "make lint"}


async def test_commands_run_first_and_the_checks_read_what_they_did(tmp_path: Path) -> None:
    """Halyard's own run, in the check's envelope — so a check comparing a
    report against the tests reads the run instead of setting out to make one."""
    a_project(tmp_path)
    asker, runner = Asking(), Running()

    handed, delivery = await hand(
        tmp_path,
        Handoff(name="close", commands=("test-fast",), inspections=("claims",)),
        asker=asker,
        project_commands=COMMANDS,
        runner=runner,
    )

    ran = "Ran test-fast: make test-fast · exit 0 · 94s · last line: 1420 passed"
    [checked] = asker.asked
    assert ran in checked
    [(_, text)] = delivery.sent
    assert f"- {ran}" in text
    assert "Command test-fast — make test-fast:\n\ncollected\n1420 passed" in text
    assert text.index("Command test-fast") < text.index("Inspection claims")
    assert [command.name for command, _ in handed.ran] == ["test-fast"]


async def test_commands_run_one_after_another_in_the_order_written(tmp_path: Path) -> None:
    a_project(tmp_path)
    runner = Running()

    await hand(
        tmp_path,
        Handoff(name="close", commands=("lint", "test-fast")),
        project_commands=COMMANDS,
        runner=runner,
    )

    assert runner.ran == ["lint", "test-fast"]


async def test_a_command_that_fails_is_reported_and_the_handoff_still_goes(
    tmp_path: Path,
) -> None:
    """Whoever receives it has to see that it failed."""
    a_project(tmp_path)
    broke = Result(ok=False, output="2 failed", seconds=31.0, exit_code=1)

    _, delivery = await hand(
        tmp_path,
        Handoff(name="close", commands=("test-fast",)),
        project_commands=COMMANDS,
        runner=Running(**{"test-fast": broke}),
    )

    [(_, text)] = delivery.sent
    assert "Ran test-fast: make test-fast · exit 1 · 31s · last line: 2 failed" in text
    assert "Command test-fast — make test-fast:\n\n2 failed" in text
