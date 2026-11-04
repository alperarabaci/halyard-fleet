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

import contextlib
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Long enough for a full test suite with a browser in it, because that is
#: exactly what somebody will point this at. Bounded all the same: a command
#: that has been going an hour has stopped being something you wait for. The
#: same for `/command`, a transition and a commit option; a command can name
#: its own with `timeout:` in `commands:`.
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

#: How long a command that is being stopped gets to end itself, after the
#: Ctrl-C somebody at the desk would press, before what is left of it is
#: killed. A test runner that is interrupted runs its teardowns, and the
#: servers and browsers it started for the suite go with it. Killed outright it
#: runs none: a stopped E2E run left its preview server running for nobody.
STOP_GRACE = 15.0

logger = logging.getLogger(__name__)

#: Terminal colour, which a phone renders as litter.
#:
#: `CI=1` in the environment silences most tools, and does nothing for a
#: Makefile that writes the escapes itself — measured on the project this was
#: built for, whose `make help` came back as `\x1b[36mtest-web\x1b[0m`. So the
#: output is cleaned rather than politely asked to be clean.
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")

#: What a shell says when it cannot find a program.
#:
#: Every shell phrases it differently, and the first version of this was
#: written from macOS alone and would have logged nothing on Linux — CI caught
#: it, which is the argument for CI running somewhere other than the machine
#: the code was written on:
#:
#:     zsh:1: command not found: uv
#:     /bin/sh: 1: uv: not found
#:     make: uv: No such file or directory
#:
#: So `not found` bare, rather than either of the two spellings that put a
#: colon on a different side of it. It over-matches a little — a test suite
#: whose output says "fixture not found" gets one extra log line — and that is
#: the cheaper error by a wide margin: under-matching costs the whole point of
#: the line, on the platform nobody here is testing on by hand.
_LOOKS_MISSING = re.compile(r"(no such file or directory|not found)", re.IGNORECASE)

#: What a shell exits with when it could not find the program at all. POSIX,
#: and the same everywhere — where the wording is not. It does not cover the
#: case this was written for, because `make` catches the failure and exits 2 on
#: its own, which is why both tests are needed rather than either.
NOT_FOUND = 127


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
    #: What it exited with. None when it never started, or was stopped.
    exit_code: int | None = None
    #: True when somebody stopped it from the chat. Like a run out of time,
    #: nothing is known about how it would have ended.
    stopped: bool = False
    #: The limit it ran out of, in seconds, when it ran out of time — said with
    #: it, so a stop at ten minutes reads as a limit and not as a hang.
    limit: float | None = None


def limit_said(seconds: float) -> str:
    """A limit as it was likely written: `45 s`, `30 min`, `1 h`, `1 h 30 min`."""
    whole = round(seconds)
    if whole != seconds or whole % 60:
        return f"{seconds:g} s"
    hours, minutes = divmod(whole // 60, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


#: How a command ended, as the line after it says and a transition reads it.
PASSED, FAILED, NOT_RUN = "passed", "failed", "not run"


def outcome(result: Result, not_run: Sequence[int] = ()) -> str:
    """Passed, failed, or not run — which is no result at all.

    Failed is a result: something ran and said red, and whoever reads it next
    decides what that means. Not run is the absence of one, and reading
    inspections over it would find green in nothing. So it is told apart:

    - by Halyard, for every command: it could not start, was stopped at the
      clock, was not found or could not be executed (127, 126), or was ended by
      a signal (128 and up, or the negative code Python reports one with);
    - by the command, for the codes it declares under `not_run:` — only it can
      know that its 2 means "nothing here to run" and not "red".
    """
    code = result.exit_code
    if result.timed_out or result.stopped or code is None:
        return NOT_RUN
    if code == 0:
        return PASSED
    if code in (126, 127) or code >= 128 or code < 0 or code in not_run:
        return NOT_RUN
    return FAILED


def _tail(text: str, lines: int) -> str:
    plain = _ANSI.sub("", text or "")
    kept = [line.rstrip() for line in plain.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def _environment() -> dict[str, str]:
    """The shell's environment, with anything interactive shut off, and without
    Halyard's own virtual environment.

    Nothing running this has a terminal. A `make` target that shells out to git
    for a credential, or a test runner that offers to open a browser, would
    otherwise block until the timeout for an answer nobody is there to give.

    Halyard runs inside a virtual environment of its own, and the project's
    commands were handed it: `VIRTUAL_ENV`, and its `bin` first on `PATH`.
    Measured on a project's e2e run: uv warned that `VIRTUAL_ENV` did not match
    the project's `.venv` on every run, and that warning was the only line the
    run printed, so the progress said it over and over. A bare `python` in a
    project's makefile would have found Halyard's.
    """
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "CI": "1",
    }
    if own := environment.pop("VIRTUAL_ENV", None):
        environment.pop("VIRTUAL_ENV_PROMPT", None)
        own_bin = os.path.join(own, "bin")
        environment["PATH"] = os.pathsep.join(
            part
            for part in environment.get("PATH", "").split(os.pathsep)
            if part and os.path.normpath(part) != os.path.normpath(own_bin)
        )
    return environment


def _end(process: subprocess.Popen) -> None:
    """End a command and everything it started, the way somebody at the desk would.

    First the interrupt Ctrl-C sends, to the whole of it, so it can tidy up
    after itself: a test runner's teardowns close what it started outside its
    own process group, where nothing else can reach it. Then, once all of it
    has ended or `STOP_GRACE` has passed, what is left is killed: whatever
    ignored the interrupt, or was still winding down.

    The shell alone is not the command: `make e2e` is a shell, make, a test
    runner and a browser. Killing the first left the rest running for nobody —
    a run stopped at its deadline went on until the machine was restarted. So
    the wait is for the whole group, not the shell: the shell can end at the
    interrupt while the runner under it is still tearing down.
    """
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGINT)
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline:
        # Reaped once it has gone, or the shell would count as still there.
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(POLL_EVERY)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(OSError):
            process.kill()


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
    stop: threading.Event | None = None,
) -> Result:
    """Run it in `path`, and say what happened.

    `stop`, once set, ends it and everything it started — `/stop` from a chat.

    `on_progress` is called every `PROGRESS_EVERY` seconds with how long it has
    been going and the newest line of output — from this thread, not the
    caller's, so whoever passes one is responsible for getting it back to where
    it belongs.

    Never raises. Every caller is somewhere a person is waiting for an answer,
    and "it could not be started" is an answer where a traceback is not.
    """
    started = time.monotonic()
    environment = _environment()
    # Said before anything can go wrong, so a run that never reports back has
    # still left a line saying it began — whoever asked for it, from wherever.
    logger.info("Command started in %s: %s", path, command)
    try:
        # A command line from this machine's own configuration, run as written.
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            # A group of its own, so the whole of it can be ended. See `_end`.
            start_new_session=True,
        )
    except OSError as refused:
        return Result(ok=False, output=str(refused), seconds=time.monotonic() - started)

    lines: list[str] = []
    reader = threading.Thread(target=_drain, args=(process.stdout, lines), daemon=True)
    reader.start()

    said_at = started
    while process.poll() is None:
        now = time.monotonic()
        if stop is not None and stop.is_set():
            _end(process)
            process.wait()
            # What an interrupted runner printed on its way out — where it had
            # got to — is the last of what it said.
            reader.join(timeout=2.0)
            logger.warning(
                "Command stopped from the chat after %.0fs in %s: %s", now - started, path, command
            )
            return Result(
                ok=False,
                output=_tail("\n".join(lines), LINES_WHEN_IT_FAILED),
                seconds=now - started,
                stopped=True,
            )
        if now - started > timeout:
            _end(process)
            process.wait()
            # What it had printed, as a failed run's is kept: where a suite
            # stopped at its limit had got to is the first thing to ask.
            reader.join(timeout=2.0)
            logger.warning("Command stopped after %.0fs in %s: %s", timeout, path, command)
            return Result(
                ok=False,
                output=_tail("\n".join(lines), LINES_WHEN_IT_FAILED)
                or f"It printed nothing before its {limit_said(timeout)} limit.",
                seconds=now - started,
                timed_out=True,
                limit=timeout,
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
        logger.info("Command finished in %.1fs: %s", seconds, command)
        return Result(
            ok=True, output=_tail(whole, LINES_WHEN_IT_PASSED), seconds=seconds, exit_code=0
        )

    logger.warning(
        "Command failed (exit %s) after %.1fs in %s: %s",
        process.returncode,
        seconds,
        path,
        command,
    )
    # A tool the shell could not find is an environment fault, not a command
    # fault, and the two read identically on a phone: `make: uv: No such file
    # or directory` says nothing about *which* uv was looked for or where.
    #
    # This happened, and cost an evening. Halyard ran as a launchd agent whose
    # PATH was a fixed list that did not include `~/.local/bin`, where uv's own
    # installer puts it. The service was fine — it is started by absolute path
    # — so nothing anywhere was broken except the environment handed to the
    # things it ran. `halyard doctor` checks that now; this is what makes the
    # next one diagnosable from the log alone.
    if process.returncode == NOT_FOUND or _LOOKS_MISSING.search(whole):
        logger.warning(
            "That reads like something was not on PATH. The PATH this ran with was: %s",
            environment.get("PATH", "(unset)"),
        )
    return Result(
        ok=False,
        output=_tail(whole, LINES_WHEN_IT_FAILED),
        seconds=seconds,
        exit_code=process.returncode,
    )


def summary(name: str, line: str, result: Result, not_run: Sequence[int] = ()) -> str:
    """One line saying what a command did, for whoever reads it next.

    Which command, how it ended — the exit code and what it means, in words, so
    nobody reading the line has to guess — how long it took, and the last thing
    it printed: where a test runner puts its count, and where a project's own
    target puts its status and the file the whole run went to. What it printed
    before that travels separately, for whoever needs more than the line.
    """
    ended = outcome(result, not_run)
    if result.stopped:
        how = f"stopped from the chat after {result.seconds:.0f}s ({ended})"
    elif result.timed_out and result.limit is not None:
        how = f"stopped after its {limit_said(result.limit)} limit ({ended})"
    elif result.timed_out:
        how = f"stopped after {result.seconds:.0f}s ({ended})"
    elif result.exit_code is None:
        how = f"could not start: {result.output} ({ended})"
    else:
        how = f"exit {result.exit_code} ({ended}) · {result.seconds:.0f}s"
    last = (
        result.output.splitlines()[-1]
        if result.output and (result.exit_code is not None or result.stopped)
        else ""
    )
    return f"Ran {name}: {line} · {how}" + (f" · last line: {last}" if last else "")
