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
import time
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


def run(command: str, path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Result:
    """Run it in `path`, and say what happened.

    Never raises. Every caller is somewhere a person is waiting for an answer,
    and "it could not be started" is an answer where a traceback is not.
    """
    started = time.monotonic()
    try:
        done = subprocess.run(
            command,
            shell=True,
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_environment(),
        )
    except subprocess.TimeoutExpired:
        return Result(
            ok=False,
            output=f"It was still running after {timeout:.0f}s and was stopped.",
            seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as refused:
        return Result(ok=False, output=str(refused), seconds=time.monotonic() - started)

    seconds = time.monotonic() - started
    if done.returncode == 0:
        return Result(ok=True, output=_tail(done.stdout, LINES_WHEN_IT_PASSED), seconds=seconds)
    return Result(
        ok=False,
        output=_tail(f"{done.stdout}\n{done.stderr}", LINES_WHEN_IT_FAILED),
        seconds=seconds,
    )
