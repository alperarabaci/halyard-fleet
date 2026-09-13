"""Tests for `halyard.checks` — one check, run over a reply, through `Asker`."""

from __future__ import annotations

from pathlib import Path

from halyard import checks


class Asking:
    """An `Asker` that answers from a script and remembers what it was asked."""

    def __init__(self, says: str | None = "proof · no finding") -> None:
        self.says = says
        self.asked: list[tuple[str, str | None]] = []

    async def ask(
        self, text: str, *, timeout: float = 180.0, model: str | None = None
    ) -> str | None:
        self.asked.append((text, model))
        if self.says is None:
            raise RuntimeError("no model today")
        return self.says


async def ran(tmp_path: Path, asker: Asking, check: str = "proof.md") -> checks.Answer:
    return await checks.run(
        "proof",
        Path(check),
        project=tmp_path,
        context=["Work item: alpha-engine#355"],
        note="delivery",
        reply="42 passed",
        asker=asker,
        model="sonnet",
        timeout=5,
    )


def test_the_prompt_says_the_check_cannot_look_for_itself() -> None:
    """The turn runs apart from the project. Saying so keeps an answer that
    needed a command run from reading as though it had run one."""
    asked = checks.prompt(
        "# proof", context=["Project: alpha-engine"], note="delivery", text="42 passed"
    )

    assert asked.startswith("# proof")
    assert "cannot open files or run commands" in asked
    assert "delivery" in asked
    assert asked.endswith("42 passed")


def test_a_fenced_answer_loses_its_fence() -> None:
    assert checks.unfenced("```\nproof · no finding\n```") == "proof · no finding"
    assert checks.unfenced("proof · no finding") == "proof · no finding"


def test_what_a_seat_is_handed_can_be_read_cold() -> None:
    """What ran, on whose reply, where, what it found — and the reply itself,
    because a navigator handed the findings alone could not tell what they
    were about."""
    text = checks.handed_on(
        "proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        author="drv (driver)",
        arrived="00:21",
        context=["Work item: alpha-engine#355"],
        findings="evidence missing",
        reply="All 42 tests passed.",
    )

    assert "`proof` check on drv (driver)'s reply from 00:21" in text
    assert "Check: proof — NOTES/proof.md @ 3e8c847" in text
    assert "Where: Work item: alpha-engine#355" in text
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
    assert isinstance(asker, checks.Asker)


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
