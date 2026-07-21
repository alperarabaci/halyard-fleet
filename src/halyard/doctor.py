"""`halyard doctor` — answer "why is everything being denied" in one command.

Every piece of this system fails closed, which is correct and which makes a
misconfiguration look exactly like a working system refusing you. A bridge
pointed at the wrong port denies every command with a message about a port,
and the port is right there in a file that says something else.

This walks the same path a hook does and says which step broke, and where the
setting it used came from — because "unreachable at 8787" is not useful when
you have three keys in `.env` and two of them say 8799.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from halyard.config import Settings

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

OK = "  ok    "
WARN = "  warn  "
FAIL = "  FAIL  "


def _find_setting(key: str) -> tuple[str | None, str]:
    """Resolve a key the way a bridge does, and report where it came from."""
    sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import _settings
    except ImportError:
        return None, "bridge/_settings.py not found"
    finally:
        sys.path.pop(0)

    import os

    if os.environ.get(key):
        return os.environ[key], "the environment"
    for path in _settings._CONFIG_FILES:
        value = _settings._read_key(path, key)
        if value:
            return value, str(path)
    return None, "nowhere — using the built-in default"


def run() -> int:
    """Check the chain end to end. Returns a process exit code."""
    problems = 0
    print("Halyard doctor\n")

    # --- configuration ------------------------------------------------------
    try:
        settings = Settings()
        print(f"{OK}configuration loads")
        print(f"        channel={settings.channel.value} project={settings.project_name!r}")
        print(
            f"{OK}timeouts ordered: approval {settings.approval_timeout_seconds}s"
            f" < bridge {settings.bridge_timeout_seconds}s"
            f" < hook {settings.hook_timeout_seconds}s"
        )
        if settings.channel.decides_without_a_human:
            print(f"{WARN}channel {settings.channel.value} answers by itself — nobody is asked")
    except ValidationError as exc:
        settings = None
        problems += 1
        print(f"{FAIL}configuration is not valid")
        for error in exc.errors():
            print(f"        {'.'.join(str(p) for p in error['loc']) or '?'}: {error['msg']}")

    # --- where the bridges will look ---------------------------------------
    url, source = _find_setting("HALYARD_URL")
    url = url or "http://127.0.0.1:8787"
    print(f"\n{OK}bridges will use {url}")
    print(f"        found in: {source}")

    # --- is anything there --------------------------------------------------
    health: dict | None = None
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5) as response:
            health = json.loads(response.read())
        print(f"{OK}control plane answers at {url}")
        print(
            f"        channel={health.get('channel')} project={health.get('project')!r}"
            f" open_approvals={health.get('open_approvals')}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        problems += 1
        print(f"{FAIL}nothing answering at {url} ({exc})")
        print("        Every approval will be denied until this is reachable.")
        _suggest_ports(settings, url)

    if health and health.get("decides_without_a_human"):
        print(f"{WARN}the running control plane answers approvals by itself")

    # --- the hook scripts ---------------------------------------------------
    print()
    for name, required in (("hook.sh", True), ("hook_bridge.py", True), ("relay.py", False)):
        path = BRIDGE_DIR / name
        if not path.exists():
            problems += required
            print(f"{FAIL if required else WARN}{path} is missing")
        elif not path.stat().st_mode & 0o111:
            problems += required
            print(f"{FAIL if required else WARN}{path} is not executable (chmod +x)")
        else:
            print(f"{OK}{name} is present and executable")

    print()
    if problems:
        print(f"{problems} problem(s) found.")
    else:
        print("Everything checks out.")
    return 1 if problems else 0


def _suggest_ports(settings: Settings | None, url: str) -> None:
    """Point at the mismatch rather than leaving it to be hunted.

    The usual cause is three keys that have to agree and do not: the port the
    service binds to, the port Docker publishes it on, and the port the bridges
    look at.
    """
    if settings is None:
        return
    configured = url.rsplit(":", 1)[-1].rstrip("/")
    candidates = {str(settings.port): "HALYARD_BIND"}
    for candidate, key in candidates.items():
        if candidate != configured:
            print(
                f"        {key} says port {candidate}, but HALYARD_URL says {configured}."
                " If you run in Docker, HALYARD_URL must match HALYARD_HOST_PORT;"
                " otherwise it must match HALYARD_BIND."
            )
