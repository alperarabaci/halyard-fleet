"""Run Halyard as a launchd service that refreshes itself before it starts.

`halyard` runs in the foreground and stops when the terminal closes. For a
control plane that a wired project cannot run a command without, that is the
wrong shape: it needs to come back after a crash, and after a reboot, without
somebody remembering to start it. On macOS that is a launchd LaunchAgent, which
is also where `caffeinate` and the rest of this project already assume it lives.

**macOS only**, like keeping the machine awake. launchd is Apple's, and the
Linux equivalent (a systemd user unit) is a different file this does not write
yet — so off macOS this refuses rather than pretending.

The service does three things in order every time it starts, which is the whole
point of it over a bare `uv run halyard`:

    git pull --ff-only   bring in whatever was pushed to the branch it tracks
    uv sync              install anything the new code now depends on
    halyard serve        start the gate

The update is **fail-open**: `--ff-only` never rewinds, rebases, or touches
local changes, and if it cannot fast-forward — a dirty tree, a diverged branch,
no network — it is skipped and the last known-good code serves anyway. A broken
update must not be the reason the gate is down.

**It runs the code it pulls.** That is the point and the danger in one sentence:
whoever can push to the branch this tracks can run code on this machine at the
next restart. Point it at a remote you control, which is why `install` prints
the exact remote and branch it will pull from and asks nothing else to be taken
on trust.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

#: One service per machine — the usage model is one Halyard per machine, so
#: there is one agent and one label rather than one per project.
LABEL = "com.halyard.fleet"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def plist_path() -> Path:
    """Where a LaunchAgent lives for the current user."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    """Where the service's own output goes — including the git pull's."""
    return Path.home() / "Library" / "Logs" / "halyard-service.log"


def _repo_root(start: Path) -> Path | None:
    """The git checkout `start` sits in, or None if it is not in one.

    Read from git rather than guessed, because `uv run halyard serve` has to run
    from the checkout — that is where the project and its `halyard.yaml` are —
    and the working directory a person runs `install` from is only usually it.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def _tracking(repo: Path) -> tuple[str, str] | None:
    """The (remote, branch) this checkout would pull from, for the warning.

    None when the branch tracks nothing — in which case `git pull` has nothing
    to do and the service just serves, which is worth saying rather than
    implying an update that cannot happen.
    """
    try:
        upstream = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    remote, _, branch = upstream.partition("/")
    return (remote, branch) if branch else None


def _serve_command(repo: Path, git: str, uv: str) -> str:
    """The one shell line the agent runs: update, then serve.

    `;` between the steps, not `&&`, so a pull that cannot fast-forward or a sync
    that fails does not stop the gate from coming up on the code already here.
    `cd` is joined with `&&` because running any of it in the wrong directory is
    worse than not running it. `exec` so the serving process replaces the shell
    and launchd watches the thing that matters rather than its wrapper.
    """
    quoted_repo = shlex.quote(str(repo))
    quoted_git = shlex.quote(git)
    quoted_uv = shlex.quote(uv)
    return (
        f"cd {quoted_repo} && "
        f"{quoted_git} pull --ff-only 2>&1 ; "
        f"{quoted_uv} sync 2>&1 ; "
        f"exec {quoted_uv} run halyard serve"
    )


def render_plist(repo: Path, git: str, uv: str, log: Path) -> bytes:
    """The LaunchAgent, as launchd expects it.

    `KeepAlive` so a crash — or a machine waking to find it gone — brings it
    back. `RunAtLoad` so it is up after a reboot without anybody logging in and
    starting it. A `PATH` that includes the usual homebrew and user-local
    directories, because launchd's own is `/usr/bin:/bin` and neither `uv` nor a
    git installed by homebrew is on it.
    """
    document = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", _serve_command(repo, git, uv)],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    return plistlib.dumps(document)


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def install() -> int:
    """Write the LaunchAgent and load it.

    Idempotent: an existing agent is unloaded first, so re-running this after a
    code change reloads it rather than failing with "already loaded".
    """
    if not _is_macos():
        print(
            "halyard: `service` manages a launchd agent, which is macOS only. "
            "On Linux, run `uv run halyard serve` under a systemd user unit."
        )
        return 1

    git = shutil.which("git")
    uv = shutil.which("uv")
    if not git or not uv:
        missing = " and ".join(name for name, found in (("git", git), ("uv", uv)) if not found)
        print(f"halyard: cannot build the service — {missing} is not on PATH.")
        return 1

    repo = _repo_root(Path.cwd())
    if repo is None:
        print(
            "halyard: run this from the Halyard checkout — the service serves "
            "from the git repository, and this directory is not in one."
        )
        return 1

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Unload a previous copy before overwriting, so a reload is a reload and not
    # a second agent. Errors are ignored: "not loaded" is the state we want.
    _launchctl("unload", "-w", str(path))
    path.write_bytes(render_plist(repo, git, uv, log))
    loaded = _launchctl("load", "-w", str(path))
    if loaded.returncode != 0:
        print(f"halyard: launchctl could not load the agent: {loaded.stderr.strip()}")
        return 1

    print(f"Installed and loaded {LABEL}.")
    print(f"  plist:  {path}")
    print(f"  log:    {log}")
    tracking = _tracking(repo)
    if tracking:
        remote, branch = tracking
        print(
            f"\n  On every start it runs `git pull --ff-only` from "
            f"{remote}/{branch}, then serves.\n"
            f"  It runs whatever that pulls — keep {remote} one you control."
        )
    else:
        print(
            "\n  This branch tracks no upstream, so there is nothing to pull; "
            "it will just serve.\n  `git branch --set-upstream-to=<remote>/<branch>` "
            "to have it update itself."
        )
    print("\n  Stop it with:  halyard service uninstall")
    return 0


def uninstall() -> int:
    """Unload the agent and remove its plist. Safe when nothing is installed."""
    if not _is_macos():
        print("halyard: `service` is macOS only; there is nothing to uninstall here.")
        return 1

    path = plist_path()
    if not path.exists():
        print(f"{LABEL} is not installed.")
        return 0

    _launchctl("unload", "-w", str(path))
    path.unlink(missing_ok=True)
    print(f"Removed {LABEL}. The gate is down until you start it again.")
    return 0


def status() -> int:
    """Say whether the agent is installed and whether launchd has it loaded."""
    if not _is_macos():
        print("halyard: `service` is macOS only.")
        return 1

    path = plist_path()
    if not path.exists():
        print(f"{LABEL}: not installed. `halyard service install` to set it up.")
        return 1

    listed = _launchctl("list", LABEL)
    if listed.returncode == 0:
        print(f"{LABEL}: installed and loaded.")
        print(f"  plist: {path}")
        print(f"  log:   {log_path()}")
        return 0
    print(f"{LABEL}: installed but not loaded. `halyard service install` to reload it.")
    return 1


def run(argument: str | None) -> int:
    """Dispatch `halyard service [install|uninstall|status]`."""
    actions = {"install": install, "uninstall": uninstall, "status": status}
    action = actions.get(argument or "status")
    if action is None:
        print(f"halyard: unknown service command {argument!r}. Use install, uninstall, or status.")
        return 2
    return action()
