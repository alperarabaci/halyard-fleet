"""What `send` is answering, and when.

The fault this file exists for left no trace in any test, because every test
used a fake process that exited immediately — and a process that exits
immediately cannot tell "answered on acceptance" apart from "answered on
completion". The two only diverge for a turn that takes a while, which is
every real one.

So the processes here are slow on purpose. Each test that matters holds one
open, asks what was reported while it was still running, and only then lets it
finish.
"""

from __future__ import annotations

import asyncio

import pytest

from halyard.agents.turns import Turns

pytestmark = pytest.mark.asyncio

# Short enough to keep the suite quick, long enough that the event loop is not
# racing the assertions. The real values are twenty seconds and four hours.
QUICKLY = 0.05


class FakeProcess:
    """A CLI that finishes when it is told to, or not at all."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        until: asyncio.Event | None = None,
    ) -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._until = until
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._until is not None:
            await self._until.wait()
        self.returncode = self._returncode
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int | None:
        return self.returncode


def starting(monkeypatch, *processes: FakeProcess) -> list[list[str]]:
    """Hand out `processes` in order, recording the command lines."""
    waiting = list(processes)
    started: list[list[str]] = []

    async def fake_exec(*arguments, **_kwargs):
        started.append(list(arguments))
        return waiting.pop(0) if waiting else FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return started


async def settles() -> None:
    """Let the turn task run to wherever it gets to."""
    for _ in range(20):
        await asyncio.sleep(0)


def turns(**kwargs) -> Turns:
    return Turns("test", accepted_after=QUICKLY, wedged_after=QUICKLY * 40, **kwargs)


# --- what the answer means ----------------------------------------------------


async def test_a_turn_still_running_is_reported_as_delivered(monkeypatch) -> None:
    """The fix, in one assertion.

    Measured on 8 September: a message forwarded at 16:58:35 was picked up,
    worked on for fifteen minutes with approval cards arriving the whole time,
    and reported at 17:13:35 as one that never arrived.
    """
    working = asyncio.Event()
    starting(monkeypatch, FakeProcess(until=working))
    turning = turns()

    assert await turning.start("s-1", ["claude", "-p"]) is True

    working.set()
    await settles()


async def test_a_message_refused_at_startup_is_still_reported_as_refused(monkeypatch) -> None:
    """The window has to keep the answer it was already getting right."""
    starting(
        monkeypatch, FakeProcess(returncode=1, stdout=b"No conversation found with session ID")
    )
    turning = turns()

    assert await turning.start("s-1", ["claude", "-p"]) is False
    assert "No conversation found" in (turning.last_error("s-1") or "")


async def test_a_turn_that_finishes_inside_the_window_is_delivered(monkeypatch) -> None:
    starting(monkeypatch, FakeProcess(returncode=0))

    assert await turns().start("s-1", ["claude", "-p"]) is True


async def test_the_reason_is_the_end_of_what_was_printed(monkeypatch) -> None:
    """A banner first, the reason last — `said_by_a_process` decided this, and
    a refusal has to keep going through it."""
    banner = b"\n".join([b"OpenAI Codex v0.145.0", b"model: gpt-5.6-terra"] * 20)
    starting(monkeypatch, FakeProcess(returncode=1, stderr=banner + b"\nrate limit reached"))
    turning = turns()

    await turning.start("s-1", ["codex", "exec"])

    assert "rate limit reached" in (turning.last_error("s-1") or "")


async def test_a_cli_that_cannot_be_started_is_not_delivered(monkeypatch) -> None:
    async def refuses(*_arguments, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuses)

    assert await turns().start("s-1", ["claude", "-p"]) is False


# --- what happens after the answer --------------------------------------------


async def test_a_turn_that_fails_after_acceptance_says_so(monkeypatch) -> None:
    """Codex running out of usage halfway through is the case that matters.

    Before this, the same sentence covered both — so the reason was carried,
    under a heading that said the message had never arrived. Now the reason is
    still carried and the heading is true.
    """
    working = asyncio.Event()
    starting(
        monkeypatch,
        FakeProcess(returncode=1, stderr=b"usage limit reached; resets at 4:26 PM", until=working),
    )
    said: list[str] = []
    turning = turns()

    async def stopped(reason: str) -> None:
        said.append(reason)

    assert await turning.start("s-1", ["codex", "exec"], when_done=stopped) is True
    assert said == [], "nothing has gone wrong yet"

    working.set()
    await settles()

    assert said and "resets at 4:26 PM" in said[0]
    assert "resets at 4:26 PM" in (turning.last_error("s-1") or "")


async def test_a_turn_that_ends_well_says_nothing(monkeypatch) -> None:
    """`when_done` is for failures. A reply arrives by its own path — the Stop
    hook and the relay — and a second message announcing the same turn would be
    noise in the one place noise costs the most."""
    working = asyncio.Event()
    starting(monkeypatch, FakeProcess(returncode=0, until=working))
    said: list[str] = []
    turning = turns()

    await turning.start("s-1", ["claude", "-p"], when_done=lambda reason: said.append(reason))  # type: ignore[arg-type,func-returns-value]
    working.set()
    await settles()

    assert said == []
    assert turning.last_error("s-1") is None


async def test_a_channel_that_cannot_be_reached_does_not_break_the_turn(monkeypatch) -> None:
    """Telegram is down often enough to matter, and the turn is over either
    way. Losing the report is a cost; an exception logged against a runtime
    that did nothing wrong is a wrong answer."""
    working = asyncio.Event()
    starting(monkeypatch, FakeProcess(returncode=1, until=working))
    turning = turns()

    async def unreachable(_reason: str) -> None:
        raise RuntimeError("no network")

    await turning.start("s-1", ["claude", "-p"], when_done=unreachable)
    working.set()
    await settles()

    assert turning.last_error("s-1") is not None


# --- a wedged turn ------------------------------------------------------------


async def test_a_turn_that_never_ends_is_eventually_stopped(monkeypatch) -> None:
    """The cap still exists. It is now high enough that reaching it is
    evidence of a fault rather than evidence of a careful person."""
    never = asyncio.Event()
    process = FakeProcess(until=never)
    starting(monkeypatch, process)
    said: list[str] = []
    turning = Turns("test", accepted_after=QUICKLY, wedged_after=QUICKLY * 2)

    async def stopped(reason: str) -> None:
        said.append(reason)

    assert await turning.start("s-1", ["claude", "-p"], when_done=stopped) is True

    await asyncio.sleep(QUICKLY * 4)
    assert process.killed
    assert said and "Stopped after" in said[0]


# --- the lock outlives the answer ---------------------------------------------


async def test_the_session_stays_busy_until_the_turn_really_ends(monkeypatch) -> None:
    """`send` returns early now, and `busy` must not follow it out.

    `/status` reads this, and so does the message that tells somebody their
    turn is queued. A session reported idle while a turn is running in it is
    the same wrong answer this whole file is about, one layer up.
    """
    working = asyncio.Event()
    starting(monkeypatch, FakeProcess(until=working))
    turning = turns()

    await turning.start("s-1", ["claude", "-p"])
    assert turning.busy("s-1") is True

    working.set()
    await settles()

    assert turning.busy("s-1") is False


async def test_a_second_message_waits_for_the_first_turn(monkeypatch) -> None:
    """Two `--resume` processes on one conversation is how a turn gets lost
    with nothing raised anywhere. Answering early must not let that in."""
    first = asyncio.Event()
    started = starting(monkeypatch, FakeProcess(until=first), FakeProcess())
    turning = turns()

    await turning.start("s-1", ["claude", "-p", "first"])
    queued = asyncio.ensure_future(turning.start("s-1", ["claude", "-p", "second"]))
    await settles()

    assert len(started) == 1, "the second turn started while the first was running"

    first.set()
    assert await asyncio.wait_for(queued, timeout=1) is True
    assert len(started) == 2


async def test_two_sessions_do_not_wait_for_each_other(monkeypatch) -> None:
    working = asyncio.Event()
    started = starting(monkeypatch, FakeProcess(until=working), FakeProcess(until=working))
    turning = turns()

    await turning.start("s-1", ["claude", "-p"])
    await turning.start("s-2", ["claude", "-p"])

    assert len(started) == 2

    working.set()
    await settles()
