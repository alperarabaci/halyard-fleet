"""Running one of a project's own commands.

The primitive both callers need. `/command` runs what somebody picked from a
list, and `commits.validation` runs the check a project has to pass before a
commit is offered — the same act with different consequences, so it is written
once here and the meaning is added by whoever called.

Through a shell, deliberately. What is configured is a command line somebody
types — `make test-fast`, `npm test && npm run lint` — and splitting it into an
argument list would refuse half of what people mean by it. It comes from this
machine's own `halyard.yaml`, which is the same trust as the file that says
where the code is.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Long enough for a full test suite with a browser in it, because that is
#: exactly what somebody will point this at. Bounded all the same: a command
#: that has been going an hour has stopped being something you wait for.
DEFAULT_TIMEOUT = 3600.0

#: How much of a run to carry back. Little on success, because "it passed" is
#: the whole message; more on failure, because a test runner puts what broke at
#: the end and one line of it is a riddle.
LINES_WHEN_IT_PASSED = 5
LINES_WHEN_IT_FAILED = 25

#: How often to say where a long run has got to.
#:
#: Somebody who pressed `/commit` is waiting for a card and cannot do anything
#: else with that thread — silence for three minutes reads as nothing having
#: happened. Not more often than this: the point is to show movement, and a line
#: every five seconds is a second wall of text.
PROGRESS_EVERY = 45.0

#: How often the run is looked in on. Short enough that a timeout is honoured
#: promptly, long enough that waiting costs nothing.
POLL_EVERY = 0.2

#: Terminal colour, which a phone renders as litter.
#:
#: `CI=1` in the environment silences most tools, and does nothing for a
#: Makefile that writes the escapes itself — measured on the project this was
#: built for, whose `make help` came back as `\x1b[36mtest-web\x1b[0m`. So the
#: output is cleaned rather than politely asked to be clean.
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


@dataclass(frozen=True)
class Result:
    """What a command did."""

    ok: bool
    #: The tail of what it printed, already cut to a readable length.
    output: str
    seconds: float
    #: True when it was still going when the clock ran out. Told apart from a
    #: plain failure: nothing is known about whether it would have passed.
    timed_out: bool = False


def _tail(text: str, lines: int) -> str:
    plain = _ANSI.sub("", text or "")
    kept = [line.rstrip() for line in plain.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def _environment() -> dict[str, str]:
    """The shell's environment, with anything interactive shut off.

    Nothing running this has a terminal. A `make` target that shells out to git
    for a credential, or a test runner that offers to open a browser, would
    otherwise block until the timeout for an answer nobody is there to give.
    """
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "CI": "1",
    }


def _drain(stream, into: list[str]) -> None:
    """Read a process's output as it appears, on a thread of its own.

    Reading has to happen while the process runs, not after: a command that
    fills the pipe buffer and is never read from blocks forever, and `make` with
    a test suite behind it fills it in seconds.
    """
    try:
        for line in stream:
            into.append(line.rstrip())
    except (OSError, ValueError):
        return


def run(
    command: str,
    path: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> Result:
    """Run it in `path`, and say what happened.

    `on_progress` is called every `PROGRESS_EVERY` seconds with how long it has
    been going and the newest line of output — from this thread, not the
    caller's, so whoever passes one is responsible for getting it back to where
    it belongs.

    Never raises. Every caller is somewhere a person is waiting for an answer,
    and "it could not be started" is an answer where a traceback is not.
    """
    started = time.monotonic()
    try:
        # A command line from this machine's own configuration, run as written.
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_environment(),
        )
    except OSError as refused:
        return Result(ok=False, output=str(refused), seconds=time.monotonic() - started)

    lines: list[str] = []
    reader = threading.Thread(target=_drain, args=(process.stdout, lines), daemon=True)
    reader.start()

    said_at = started
    while process.poll() is None:
        now = time.monotonic()
        if now - started > timeout:
            process.kill()
            process.wait()
            return Result(
                ok=False,
                output=f"It was still running after {timeout:.0f}s and was stopped.",
                seconds=now - started,
                timed_out=True,
            )
        if on_progress and now - said_at >= PROGRESS_EVERY:
            said_at = now
            latest = _tail("\n".join(lines), 1)
            if latest:
                on_progress(now - started, latest)
        time.sleep(POLL_EVERY)

    # Whatever was still in flight when it exited. Bounded, because a reader
    # that cannot finish must not hold the answer hostage.
    reader.join(timeout=2.0)
    seconds = time.monotonic() - started
    whole = "\n".join(lines)
    if process.returncode == 0:
        return Result(ok=True, output=_tail(whole, LINES_WHEN_IT_PASSED), seconds=seconds)
    return Result(ok=False, output=_tail(whole, LINES_WHEN_IT_FAILED), seconds=seconds)
