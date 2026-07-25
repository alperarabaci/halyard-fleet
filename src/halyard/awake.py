"""Keep the machine awake for as long as the gate is the gate.

A wired project cannot run a command without an answer from this process. So a
control plane that sleeps does not pause — it takes every session on that
machine with it, and the way that shows up is not an error message.

Measured on a Mac mini running Halyard, with `pmset -g assertions`:

    pid 773(screensharingd)  PreventSystemSleep         "Remote user is connected"
    pid 110(powerd)          PreventUserIdleSystemSleep "Prevent sleep while display is on"

Those were the only two, and both belonged to a screen-sharing session opened
from another machine. Halyard held nothing. When that connection dropped the
last assertion went with it, the mini slept, and approvals stopped arriving on
the phone and started appearing in the desktop app instead — the runtime's own
prompt, because the hook never got an answer.

**Only while serving, and only idle sleep.** The assertion is tied to this
process: it is released on shutdown and dies with a kill, so nothing is left
holding a machine awake for a control plane that is no longer running. The
display may sleep, the lid may close, and a person may still choose Sleep from
the menu. This says the machine should not drift off on its own while something
depends on it.

macOS only, and never fatal. `caffeinate` ships with the system; if it is
missing or refuses, that is worth one line in the log and nothing more. A gate
that would not start because it could not prevent a nap would be worse than the
nap.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: `-i` prevents idle *system* sleep. Not `-d`, which would also keep the
#: display awake, and not `-s`, which ignores whether the machine is on
#: battery. `-w <pid>` ties its life to this process, so a control plane that
#: is killed outright does not leave an assertion behind it.
ARGUMENTS = ("caffeinate", "-i", "-w")


@contextlib.contextmanager
def held(enabled: bool = True) -> Iterator[bool]:
    """Hold an idle-sleep assertion for the duration of the block.

    Yields whether one is actually held, so the caller can say so rather than
    claim something it did not manage.
    """
    if not enabled or sys.platform != "darwin" or shutil.which("caffeinate") is None:
        yield False
        return

    try:
        process = subprocess.Popen(
            [*ARGUMENTS, str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        logger.warning("Could not keep this machine awake (%s); it may sleep while serving", error)
        yield False
        return

    try:
        yield True
    finally:
        # Ended here rather than left to `-w` to notice, so a restart cannot
        # briefly leave two of them.
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=5)
