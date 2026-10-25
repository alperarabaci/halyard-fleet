"""A workflow run's card, saying what its step is doing while it does it.

A step can take an hour before anybody hears from it: its commands, then its
inspections, then the seat's own turn. The run's one card said only that the
step had started — "running test-fast then inspecting claims first…" — and a
command that printed nothing said nothing more, so a phone could not tell a
test run twelve minutes in from one that had hung, or a step waiting on its
agent from one that never got there.

So the card carries a line of its own under what it said, rewritten as the
step moves and as time passes:

    close_x — running test-fast · 12 min
    close_x — inspecting · 4 of 6 · 3 min
    close_x — on its way to xnav · 1 min
    close_x — waiting for xnav · 25 min

The time is how long the step has been at what the line says, read from a clock
rather than from anything a command prints. It moves once a minute, and once
every ten after the first hour: a run can wait on its agent all night, and a
card rewritten every minute of it would be noise for Telegram as well. The card
is only ever rewritten while it is open: once the run has moved on, or stopped,
its next card has superseded this one, and the line stops with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections.abc import Callable

from halyard.channels.telegram.desk import Card, CardDesk

logger = logging.getLogger(__name__)

#: How often the time on the line moves.
TICK_SECONDS = 60.0
#: How often once the step has been at the same thing for `SLOW_AFTER_SECONDS`.
SLOW_TICK_SECONDS = 600.0
SLOW_AFTER_SECONDS = 3600.0

#: How much of a command's newest line the card shows.
LATEST_CHARS = 150


def took(seconds: float) -> str:
    """`under a minute`, `12 min`, `2 h 05 min` — minutes, since the line
    moves once a minute."""
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class StepStatus:
    """The line under a run's card for one step, and the clock that moves it.

    Told what the step is doing — `running`, `inspecting`, `on_its_way`,
    `waiting` — and shown at once; between those, `start` moves the time on
    the card until the card is closed or `stop` is called. `printed` keeps a
    command's newest line, from whatever thread the command runs in, for the
    next time the card is shown.
    """

    def __init__(
        self,
        desk: CardDesk,
        card: Card,
        step: str,
        *,
        inspections: int = 0,
        clock: Callable[[], float] = time.monotonic,
        tick: float = TICK_SECONDS,
    ) -> None:
        self._desk = desk
        self._card = card
        #: What the card said before any line was put under it.
        self._head = card.html
        self._step = step
        self._clock = clock
        self._tick = tick
        self._inspections = inspections
        self._answered = 0
        self._doing = ""
        self._since = clock()
        self._latest = ""
        self._ticking: asyncio.Task | None = None

    # --- what the step is doing --------------------------------------------

    async def running(self, command: str) -> None:
        await self._now(f"running {command}")

    def printed(self, latest: str) -> None:
        """A command's newest line. Kept, not shown: called from the thread
        the command runs in, and the next tick shows it."""
        self._latest = latest.strip()[:LATEST_CHARS]

    async def inspecting(self) -> None:
        """The inspections have started; none has answered yet."""
        if not self._doing.startswith("inspecting"):
            await self._now(self._inspected())

    async def inspection_answered(self) -> None:
        """One more inspection has answered, however it ended."""
        self._answered = min(self._answered + 1, self._inspections)
        self._doing = self._inspected()
        with contextlib.suppress(Exception):
            await self.show()

    async def on_its_way(self, seat: str) -> None:
        await self._now(f"on its way to {seat}")

    async def waiting(self, seat: str) -> None:
        """Its agent has the step. Said again, the time is kept."""
        if self._doing != f"waiting for {seat}":
            await self._now(f"waiting for {seat}")

    # --- the card ----------------------------------------------------------

    def line(self) -> str:
        """The line under the card, as HTML — empty before the step does anything."""
        if not self._doing:
            return ""
        said = (
            f"⏳ {html.escape(self._step)} — {html.escape(self._doing)} · "
            f"{took(self._clock() - self._since)}"
        )
        if self._latest and self._doing.startswith("running"):
            said += f"\n<code>{html.escape(self._latest)}</code>"
        return said

    def text(self) -> str:
        """The card's whole text: what it said, and the line under it."""
        line = self.line()
        return f"{self._head}\n\n{line}" if line else self._head

    async def show(self) -> bool:
        """Rewrite the card with the line as it stands. Whether it is still
        open — a closed card is never rewritten."""
        return await self._desk.update(self._card, self.text())

    def start(self) -> None:
        """Move the time on the card until it closes."""
        if self._ticking is None:
            self._ticking = asyncio.create_task(self._tick_on(), name=f"status of {self._step}")

    def stop(self) -> None:
        if self._ticking is not None:
            self._ticking.cancel()
            self._ticking = None

    async def _tick_on(self) -> None:
        while True:
            at_it = self._clock() - self._since
            await asyncio.sleep(SLOW_TICK_SECONDS if at_it >= SLOW_AFTER_SECONDS else self._tick)
            try:
                if not await self.show():
                    return
            except Exception:
                # Cosmetic. The step goes on whatever its card says.
                logger.debug("Could not move the time on %s", self._step, exc_info=True)

    async def _now(self, doing: str) -> None:
        self._doing = doing
        self._since = self._clock()
        self._latest = ""
        with contextlib.suppress(Exception):
            await self.show()

    def _inspected(self) -> str:
        return f"inspecting · {self._answered} of {self._inspections}"
