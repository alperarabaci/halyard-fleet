"""Tests for putting each seat's label on the task its branch is for.

What makes the record worth keeping: one label per seat, written once, never
guessed, and never on the path of an approval.
Driven through a real `SessionRegistry`, because being told about a session is
half of what is under test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from halyard.core.events import Role
from halyard.core.registry import SessionRegistry
from halyard.core.seats import Seat
from halyard.tasks import attribution
from halyard.tasks.attribution import Attribution, label_for
from halyard.tasks.spec import ForgeError, Task

pytestmark = pytest.mark.asyncio


class FakeForge:
    """A tracker that remembers what it was asked."""

    def __init__(self, labels=(), refuse: bool = False) -> None:
        self.labels = list(labels)
        self.refuse = refuse
        self.asked = 0
        self.added: list[tuple[int, str]] = []

    async def task(self, number: int) -> Task:
        self.asked += 1
        if self.refuse:
            raise ForgeError("403 Forbidden: this token may not label issues")
        return Task(number=number, title="a task", labels=tuple(self.labels))

    async def add_label(self, number: int, label: str) -> Task:
        self.added.append((number, label))
        self.labels.append(label)
        return Task(number=number, title="a task", labels=tuple(self.labels))


@dataclass
class Opted:
    """A project that asked for this, as far as `Attribution` looks."""

    path: Path
    forge: str | None = None
    seats: tuple = ()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    where = tmp_path / "alpha-engine"
    (where / "src").mkdir(parents=True)
    return where


def wired(
    monkeypatch, repo: Path, forge, *, branch: str = "347-2026-09-07-power-gen-fixes", seats=()
):
    monkeypatch.setattr(attribution, "current_branch", lambda path: branch)
    monkeypatch.setattr(attribution, "origin_of", lambda path: object())
    monkeypatch.setattr(attribution, "build", lambda origin, token, declared=None: forge)
    labeller = Attribution(
        token="a-token",
        projects={"alpha-engine": Opted(path=repo, seats=tuple(seats))},
        tag_of={"claude-code": "claude", "codex": "codex"}.get,
    )
    registry = SessionRegistry()
    registry.listen(labeller.seen)
    return registry, labeller


async def settled(labeller: Attribution) -> None:
    while labeller._running:
        await asyncio.gather(*list(labeller._running))


async def work(registry, cwd: Path, *, session="s-nav", runtime="claude-code", role=Role.NAVIGATOR):
    await registry.observe(
        session_id=session, agent_id=runtime, project="alpha-engine", role=role, cwd=str(cwd)
    )


async def test_the_label_is_runtime_and_role_with_one_colon() -> None:
    """One colon: GitLab's paid tiers read `::` as a scoped label, where one
    runtime's two roles on a task would replace each other. And one label per
    seat, whatever the session happens to be called on this machine."""
    assert label_for("claude", Role.NAVIGATOR) == "claude:navigator"
    assert label_for("codex", Role.REVIEWER) == "codex:reviewer"
    assert label_for("claude", None) == "claude"


async def test_a_seat_that_starts_work_labels_the_task_its_branch_is_for(monkeypatch, repo) -> None:
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge)

    await work(registry, repo / "src")
    await settled(labeller)

    assert forge.added == [(347, "claude:navigator")]


async def test_a_label_already_on_the_task_is_not_written_again(monkeypatch, repo) -> None:
    """Read first, and never rely on what the tracker does with a duplicate."""
    forge = FakeForge(labels=["postmortem", "claude:navigator"])
    registry, labeller = wired(monkeypatch, repo, forge)

    await work(registry, repo)
    await settled(labeller)

    assert forge.added == []


async def test_a_seat_heard_from_again_does_not_ask_the_tracker_again(monkeypatch, repo) -> None:
    """A session's hundredth turn must cost nothing but a branch lookup."""
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge)

    for _ in range(3):
        await work(registry, repo)
        await settled(labeller)

    assert forge.asked == 1
    assert forge.added == [(347, "claude:navigator")]


async def test_each_seat_adds_its_own_label(monkeypatch, repo) -> None:
    """A prompt written at the navigator and forwarded to the reviewer: two
    seats on one task, and the task says so."""
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge)

    await work(registry, repo, session="s-nav", runtime="claude-code", role=Role.NAVIGATOR)
    await work(registry, repo, session="s-rev", runtime="codex", role=Role.REVIEWER)
    await settled(labeller)

    assert sorted(forge.added) == [(347, "claude:navigator"), (347, "codex:reviewer")]


async def test_without_roles_the_runtime_is_the_label(monkeypatch, repo) -> None:
    """One seat and no roles is a whole setup, and needs nothing more to be
    counted."""
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge)

    await work(registry, repo, role=None)
    await settled(labeller)

    assert forge.added == [(347, "claude")]


async def test_a_seat_without_a_role_can_name_its_label(monkeypatch, repo) -> None:
    forge = FakeForge()
    registry, labeller = wired(
        monkeypatch,
        repo,
        forge,
        seats=[Seat("dev", "claude-code", "a-dev", None, None, task_label="developer")],
    )

    await work(registry, repo, role=None)
    await settled(labeller)

    assert forge.added == [(347, "developer")]


async def test_where_a_project_gives_roles_a_sighting_without_one_is_not_labelled(
    monkeypatch, repo
) -> None:
    """It could be any of the role seats, or a session no seat owns, and a
    guess would put a second label on one seat."""
    forge = FakeForge()
    registry, labeller = wired(
        monkeypatch, repo, forge, seats=[Seat("nav", "claude-code", "a-nav", None, Role.NAVIGATOR)]
    )

    await work(registry, repo, role=None)
    await settled(labeller)

    assert forge.asked == 0


async def test_work_outside_a_project_that_asked_is_not_labelled(
    monkeypatch, repo, tmp_path
) -> None:
    elsewhere = tmp_path / "some-other-codebase"
    elsewhere.mkdir()
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge)

    await work(registry, elsewhere)
    await settled(labeller)

    assert forge.asked == 0


async def test_a_branch_not_named_for_a_task_is_not_labelled(monkeypatch, repo) -> None:
    forge = FakeForge()
    registry, labeller = wired(monkeypatch, repo, forge, branch="main")

    await work(registry, repo)
    await settled(labeller)

    assert forge.asked == 0


async def test_a_refusal_is_swallowed_and_not_asked_again(monkeypatch, repo) -> None:
    """Usually a token that may not label issues. Asking again on every turn
    would turn one refusal into a stream of them."""
    forge = FakeForge(refuse=True)
    registry, labeller = wired(monkeypatch, repo, forge)

    for _ in range(2):
        await work(registry, repo)
        await settled(labeller)

    assert forge.asked == 1
    assert forge.added == []


async def test_being_heard_from_never_waits_for_the_tracker(monkeypatch, repo) -> None:
    """The observation is on the path of every approval. A tracker that does
    not answer must cost that approval nothing."""

    class Stalled(FakeForge):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def task(self, number: int) -> Task:
            await self.release.wait()
            return await super().task(number)

    forge = Stalled()
    registry, labeller = wired(monkeypatch, repo, forge)

    await asyncio.wait_for(work(registry, repo), timeout=1)
    assert forge.added == []

    forge.release.set()
    await settled(labeller)
    assert forge.added == [(347, "claude:navigator")]


async def test_a_seat_that_names_its_own_label_is_labelled_with_it(monkeypatch, repo) -> None:
    """A model pinned to a seat is known to whoever pinned it."""
    forge = FakeForge()
    registry, labeller = wired(
        monkeypatch,
        repo,
        forge,
        seats=[
            Seat("nav", "claude-code", "a-nav", None, Role.NAVIGATOR, task_label="fable:navigator")
        ],
    )

    await work(registry, repo)
    await settled(labeller)

    assert forge.added == [(347, "fable:navigator")]


async def test_another_seat_s_label_is_not_borrowed(monkeypatch, repo) -> None:
    """Matched on runtime and role both: the reviewer's choice is not the
    navigator's."""
    forge = FakeForge()
    registry, labeller = wired(
        monkeypatch,
        repo,
        forge,
        seats=[Seat("xrev", "codex", "a-rev", None, Role.REVIEWER, task_label="astra:reviewer")],
    )

    await work(registry, repo)
    await settled(labeller)

    assert forge.added == [(347, "claude:navigator")]
