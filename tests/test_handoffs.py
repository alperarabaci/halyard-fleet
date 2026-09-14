"""Tests for `halyard.handoffs` — a reply handed on, its checks run first."""

from __future__ import annotations

from pathlib import Path

from halyard import handoffs
from halyard.core.config_file import Handoff


class Asking:
    """An `Asker` that answers from a script and remembers what it was asked."""

    def __init__(self, says: str | None = "no finding") -> None:
        self.says = says
        self.asked: list[str] = []
        #: What each turn went by, as a card for one of its commands would say.
        self.names: list[str | None] = []

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        name: str | None = None,
        edits: bool = True,
    ) -> str | None:
        self.asked.append(text)
        self.names.append(name)
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


async def hand(tmp_path: Path, handoff: Handoff, *, asker: Asking | None = None):
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
        project_checks={"proof": Path("NOTES/proof.md"), "claims": Path("NOTES/claims.md")},
        asker=asker,
        model="sonnet",
        timeout=5,
        delivery=delivery,
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
        checks=("proof", "claims"),
        to="navigator",
    )

    handed, delivery = await hand(tmp_path, discovery, asker=asker)

    [(label, text)] = delivery.sent
    assert label == "nav"
    assert text.startswith("To nav (navigator), from Halyard — handoff: discovery.")
    order = [
        text.index("The message below"),
        text.index("Check proof"),
        text.index("Check claims"),
        text.index("All 42 tests passed."),
    ]
    assert order == sorted(order)
    assert "From: drv (driver), reply from 00:21" in text
    assert "Work item: alpha-engine#355" in text
    assert len(asker.asked) == 2
    assert [answer.name for answer in handed.answers] == ["proof", "claims"]
    assert isinstance(delivery, handoffs.Delivery)


async def test_a_check_that_could_not_run_still_goes_marked_unmeasured(tmp_path: Path) -> None:
    """An unmeasured line is not a clean one, and the reader has to see it."""
    a_project(tmp_path)

    handed, delivery = await hand(tmp_path, Handoff(name="discovery", checks=("proof",)))

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

    await hand(tmp_path, Handoff(name="discovery", checks=("proof", "claims")), asker=asker)

    assert sorted(asker.names) == ["claims · handoff discovery", "proof · handoff discovery"]


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
        Handoff(name="discovery", checks=("proof",)),
        project=tmp_path,
        context=[],
        note="",
        reply="All 42 tests passed.",
        arrived="00:21",
        sender="drv (driver)",
        recipient_label="nav",
        recipient="nav (navigator)",
        project_checks={"proof": Path("NOTES/proof.md")},
        asker=Asking(says="proof · status: candidate"),
        model="sonnet",
        timeout=5,
        delivery=Delivered(),
        findings=("status: candidate",),
        labeller=Labelling(),
    )

    assert put == ["halyard:proof"]
