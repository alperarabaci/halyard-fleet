"""Opening a desktop application, and knowing whether it is already open.

The mechanics only. Which applications exist is `catalogue`'s business, and it
keeps them in a data file — nothing in this package names an application, which
is also what keeps `tests/test_runtime_isolation.py` green, since two of those
names happen to be runtimes as well.

Everything works from a bundle id. A path would have been the obvious way and
is the wrong one: an application can be renamed, moved into a subfolder, or
installed somewhere other than `/Applications`, and the id on its `Info.plist`
survives all three. It also answers a question a filename cannot — Codex ships
as `ChatGPT.app`, which says nothing about what it is, while
`com.openai.codex` says it outright.

**Nothing here sends an Apple Event.** Asking AppleScript whether an
application is running reads as wanting to *control* it, and macOS says so:

    "uv" wants access to control "Codex Computer Use". Allowing control will
    provide access to documents and data in "Codex Computer Use", and to
    perform actions within that app.

That prompt appeared the first time somebody opened Codex from a phone — on the
desktop, where nobody was, for a permission far wider than the question. Denied,
it would answer "not running" forever; unanswered, it holds the call until it
times out. Launch Services knows the same thing and asks nothing, so it is the
only thing consulted.

macOS only, and honestly so: `open` and `lsappinfo` are what this is. A Linux
control plane simply reports that it cannot open applications, rather than
pretending with something that would not work.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from halyard.applications.catalogue import Application

logger = logging.getLogger(__name__)

#: Long enough for Spotlight to answer on a cold index, short enough that a
#: phone is not left holding while it does.
LOOKUP_TIMEOUT = 10.0

#: Launching is asynchronous — `open` returns once the application has been
#: told, not once it is on screen — so this only bounds the telling.
OPEN_TIMEOUT = 20.0


#: What macOS calls an application that is on screen, as against one that is
#: running with no Dock icon and no window.
FOREGROUND = "Foreground"


@dataclass(frozen=True)
class Status:
    """Where an application stands right now.

    `running` and `on_screen` are genuinely different, and conflating them is
    what made this report "already open" for an application that was nowhere to
    be seen. Measured: Antigravity with its last window closed still has a live
    process, its helpers and its language server — `is running` says true, quite
    correctly — while macOS has moved it to `UIElement`, which is to say no Dock
    icon and nothing on screen.

    Neither Antigravity nor Claude declares `LSUIElement` in its `Info.plist`,
    so that state is one the application entered on its own and is a fair
    reading of "there is no window".
    """

    #: Where it is installed, or None if it is not on this machine.
    path: Path | None
    #: The process exists.
    running: bool
    #: It has a foreground presence — a Dock icon, and something to look at.
    on_screen: bool = False

    @property
    def installed(self) -> bool:
        return self.path is not None


def available() -> bool:
    """Whether this machine can open applications at all."""
    return platform.system() == "Darwin"


def _ask(*command: str, timeout: float) -> str | None:
    """Run a command and return its output, or None if it failed in any way.

    Every caller here is answering a question about the desktop, and the honest
    answer to "could not ask" is "I do not know" rather than an exception on a
    path that a phone is waiting on.
    """
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def find(app: Application) -> Path | None:
    """Where this application is installed, or None.

    Spotlight first, because it finds an application wherever somebody put it.
    The declared fallback second, for a machine with indexing turned off — a
    real configuration, and one where "not installed" would be a lie.
    """
    if not available():
        return None
    found = _ask(
        "mdfind",
        f"kMDItemCFBundleIdentifier == '{app.bundle_id}'",
        timeout=LOOKUP_TIMEOUT,
    )
    for line in (found or "").splitlines():
        candidate = Path(line.strip())
        if line.strip() and candidate.exists():
            return candidate
    if app.fallback and app.fallback.exists():
        return app.fallback
    return None


def _asn(app: Application) -> str | None:
    """Launch Services' handle for this application, or None if it is not up.

    The whole running check. An application with no record is not running, and
    one with a record is — measured across all three states, including an
    application that had never been launched.
    """
    said = _ask("lsappinfo", "find", f"bundleid={app.bundle_id}", timeout=LOOKUP_TIMEOUT)
    for line in (said or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def _foreground(asn: str) -> bool:
    said = _ask("lsappinfo", "info", "-only", "ApplicationType", asn, timeout=LOOKUP_TIMEOUT)
    return FOREGROUND in (said or "")


def running(app: Application) -> bool:
    """Whether its process is up.

    Asked of Launch Services rather than the process table. Matching on a path
    breaks for an application launched from somewhere unexpected, and matching
    on a name would match a terminal that merely has the name in its command
    line.
    """
    return available() and _asn(app) is not None


def on_screen(app: Application) -> bool:
    """Whether it has a foreground presence, not merely a process.

    Counting windows would be the direct question and cannot be asked: it goes
    through System Events and needs Accessibility permission, which this would
    have to ask for at a desk for a feature whose whole point is not being at
    one. Measured — it refuses with "osascript is not allowed assistive access".
    """
    if not available():
        return False
    asn = _asn(app)
    return asn is not None and _foreground(asn)


def status(app: Application) -> Status:
    """All three questions, asking Launch Services once."""
    if not available():
        return Status(path=None, running=False, on_screen=False)
    asn = _asn(app)
    return Status(
        path=find(app),
        running=asn is not None,
        on_screen=asn is not None and _foreground(asn),
    )


def open_(app: Application) -> bool:
    """Open it, and say whether the request was accepted.

    True means macOS took the request, not that a window is on screen — `open`
    returns as soon as the application has been told. The phone is told the
    same thing, in those words, rather than a confidence nothing here has.
    """
    if not available():
        return False
    return _ask("open", "-b", app.bundle_id, timeout=OPEN_TIMEOUT) is not None
