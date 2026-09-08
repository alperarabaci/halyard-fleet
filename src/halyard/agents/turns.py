"""Running a CLI turn, and answering as soon as the message is accepted.

`AgentRunner.send` has always said it "returns whether it was accepted". Two
of the runtimes did not: they started `claude -p --resume` or `codex exec
resume` and waited for the whole turn, so the boolean meant *the work
finished* — a different question, answered fifteen minutes later, about
something nobody was waiting for.

Measured, on 8 September. A message was forwarded to the navigator at
16:58:35. It arrived: the session picked it up and the first approval card
reached the phone eighteen seconds later, and cards kept coming until 17:12:53
— a dozen of them, each one a person reading a command on a phone and deciding.
At 17:13:35.914, exactly 900.0 seconds after delivery, the turn hit the timeout
and was killed, and what reached the phone was:

    ⚠️ That did not reach ed774cad-… (claude-code).

Three things wrong at once, and the message is the worst of them. It did reach.
It reached, it worked for a quarter of an hour, and then this stopped it — and
the one sentence the person got said the opposite, which sent them looking at
the feature they had just used rather than at the clock that had just run out.

The second is what the clock was measuring. Halyard's own gate is what makes a
turn slow: every approval is a person on a phone, and the timeout's own comment
said so and then budgeted for it with a bigger number. That gets it backwards.
The more carefully somebody reads what they are approving, the more certain it
becomes that their agent is killed for it, and the failure lands on the longest
and most-supervised turns rather than the broken ones.

The third is that fifteen minutes of real work went in the bin. Edits already
made survive on disk, but whatever the turn was holding does not.

`opencode` had already reached the conclusion this module is named after,
against the same symptom thirty seconds into a turn: "what is being waited for
now is acceptance, not work". It had somewhere to put it — an HTTP endpoint
that returns on acceptance. A CLI has no such endpoint, so acceptance is read
from the one signal it does give: a message that is going to be refused is
refused quickly. A missing conversation, an expired login, a directory the
session does not belong to — every one of them is a startup check that fails in
seconds. Nothing takes a minute to say no.

So a process still running after the short window has been accepted, the person
is told so, and the turn finishes behind the answer. What remains is a cap, and
it is no longer a turn budget — it is there so a genuinely wedged process
cannot hold a session forever. It should never be reached by real work, which
is why it is hours rather than minutes.

A failure *after* acceptance still has to be reported, and by a different
route: `last_error` is too late to help — nobody asks — and "did not reach" is
now a lie. Codex running out of usage mid-turn is the case that matters, and
the reason it prints (with the time the limit resets) is worth carrying. So a
caller may hand in `when_done`, called only when an accepted turn ends badly.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence

from halyard.core.said_by_a_process import the_useful_end

logger = logging.getLogger(__name__)

#: How long to give a message to be refused.
#:
#: Everything that rejects one does it at startup: "No conversation found with
#: session ID", "Not logged in · Please run /login", a binary that is not where
#: it was. Twenty seconds is several times what any of those take on a loaded
#: machine, and it is the whole cost of the honesty — a real rejection is
#: reported this many seconds later than it used to be.
ACCEPTED_AFTER_SECONDS = 20.0

#: When to conclude a turn is wedged rather than long.
#:
#: Not a budget. A turn gated by a person can take an hour without anything
#: being wrong with it, and the number that was supposed to allow for that is
#: what killed the turn above. This is the other kind of number: high enough
#: that reaching it is evidence of a fault, because no turn anybody is waiting
#: on runs for four hours.
WEDGED_AFTER_SECONDS = 4 * 60 * 60.0

#: Told about a turn that was accepted and then failed, with the reason.
LateFailure = Callable[[str], Awaitable[None]]


class Turns:
    """One turn at a time per session, answered when it is accepted.

    Owns the per-session lock as well, because the lock has to outlive the
    answer: `send` returns while the process is still running, and a second
    message must still queue behind the first rather than start a second
    `--resume` on the same conversation.
    """

    def __init__(
        self,
        runtime: str,
        *,
        accepted_after: float = ACCEPTED_AFTER_SECONDS,
        wedged_after: float = WEDGED_AFTER_SECONDS,
    ) -> None:
        self._runtime = runtime
        self._accepted_after = accepted_after
        self._wedged_after = wedged_after
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_error: dict[str, str] = {}
        # Strong references. A task held only by the event loop can be
        # collected mid-turn, which is a turn that stops for no reason.
        self._running: set[asyncio.Task] = set()

    def busy(self, session_id: str) -> bool:
        """Whether a turn this runner started is still going in that session."""
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

    def last_error(self, session_id: str) -> str | None:
        """Why the last delivery to this session failed, if one did."""
        return self._last_error.get(session_id)

    async def start(
        self,
        session_id: str,
        arguments: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        when_done: LateFailure | None = None,
    ) -> bool:
        """Run `arguments` as a turn, returning once the message is accepted."""
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[bool] = loop.create_future()
        turn = loop.create_task(
            self._turn(session_id, list(arguments), cwd, env, accepted, when_done),
            name=f"{self._runtime}-turn-{session_id}",
        )
        self._running.add(turn)
        turn.add_done_callback(self._running.discard)
        return await accepted

    async def _turn(
        self,
        session_id: str,
        arguments: list[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        accepted: asyncio.Future[bool],
        when_done: LateFailure | None,
    ) -> None:
        try:
            async with self._locks[session_id]:
                await self._run(session_id, arguments, cwd, env, accepted, when_done)
        except Exception:
            logger.exception("A turn in %s ended badly", session_id)
        finally:
            # Whatever happened, somebody is awaiting this. An unanswered
            # future here is a chat message that never gets a reply of any
            # kind, which is the one outcome worse than a wrong one.
            if not accepted.done():
                accepted.set_result(False)

    async def _run(
        self,
        session_id: str,
        arguments: list[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        accepted: asyncio.Future[bool],
        when_done: LateFailure | None,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                # Closed rather than inherited: a resumed run warns and stalls
                # for three seconds when it is handed a stdin that never
                # produces anything.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=dict(env) if env is not None else None,
            )
        except OSError:
            logger.exception("Could not start the %s CLI", self._runtime)
            accepted.set_result(False)
            return

        # `communicate` rather than `wait`, and started now rather than after
        # the window: these CLIs print steadily, and a pipe nobody is draining
        # fills and stops the process — a wedge of our own making, which the
        # cap below would then blame on the runtime.
        reading = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(reading), timeout=self._accepted_after
            )
        except TimeoutError:
            # Still running, so it was accepted. `shield` keeps the read going
            # through the timeout; without it this would cancel the very task
            # that is draining the pipes.
            accepted.set_result(True)
        else:
            reason = self._why(process.returncode, stdout, stderr)
            accepted.set_result(reason is None)
            if reason is not None:
                self._last_error[session_id] = reason
                logger.error(
                    "Delivering a message to %s failed (exit %s): %s",
                    session_id,
                    process.returncode,
                    reason,
                )
            return

        try:
            stdout, stderr = await asyncio.wait_for(reading, timeout=self._wedged_after)
        except TimeoutError:
            reading.cancel()
            process.kill()
            await process.wait()
            reason = f"Stopped after {self._wedged_after / 3600:.0f}h with no reply."
            logger.error(
                "A turn in %s ran past %.0fs; giving up on it", session_id, self._wedged_after
            )
        else:
            reason = self._why(process.returncode, stdout, stderr)
            if reason is not None:
                logger.error(
                    "A turn in %s stopped (exit %s): %s", session_id, process.returncode, reason
                )

        if reason is None:
            return
        self._last_error[session_id] = reason
        if when_done is not None:
            # Best-effort. The turn is over either way, and a channel that
            # cannot be reached must not turn into an exception logged against
            # a runtime that did nothing wrong.
            try:
                await when_done(reason)
            except Exception:
                logger.exception("Could not report how the turn in %s ended", session_id)

    @staticmethod
    def _why(returncode: int | None, stdout: bytes | None, stderr: bytes | None) -> str | None:
        """The reason it failed, or None if it did not."""
        if returncode == 0:
            return None
        # Both streams. The CLI says "Not logged in · Please run /login" on
        # *stdout*, and reading only stderr logged `failed (exit 1):` with
        # nothing after the colon — a delivery that failed for a reason the
        # machine had printed and this threw away. The end of it, not the
        # beginning: every one of these CLIs prints a banner before it prints
        # a problem — see `said_by_a_process`.
        return (
            the_useful_end(
                (stderr or b"").decode("utf-8", "replace")
                or (stdout or b"").decode("utf-8", "replace")
            )
            or "no output"
        )
