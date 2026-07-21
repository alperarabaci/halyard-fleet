"""Where the bridges find the control plane, without being told every time.

A hook runs in whatever environment Claude Code was launched with. Requiring
`HALYARD_URL` to be exported in that shell means remembering it in every
terminal, on every machine, forever — and forgetting it does not produce a
helpful error. It produces a denied command, because the approval bridge fails
closed on a control plane it cannot reach.

So the bridges look it up instead. The address is a fact about the machine, and
it is already written down in the `.env` the control plane reads. Nothing needs
to be configured twice.

Standard library only, and it never raises: a bridge that crashes reading its
own configuration is worse than one that falls back to the default.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8787"

#: Searched in order, first hit wins. The repo's own `.env` comes first because
#: it is the file the control plane is already configured from; the home
#: location exists for installs where the bridges are referenced from elsewhere.
_CONFIG_FILES = (
    Path(__file__).resolve().parent.parent / ".env",
    Path.home() / ".halyard" / "config",
)


def _read_key(path: Path, key: str) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == key:
                    return value.strip().strip("\"'") or None
    except OSError:
        return None
    return None


def lookup(key: str, default: str) -> str:
    """Return `key` from the environment, then the config files, then `default`."""
    from_env = os.environ.get(key)
    if from_env:
        return from_env
    for path in _CONFIG_FILES:
        value = _read_key(path, key)
        if value:
            return value
    return default


def _url_from_bind(bind: str) -> str:
    """Turn `host:port` into the address a client on this machine would use.

    A server bound to every interface is still reached over loopback, and a
    bridge asked to connect to 0.0.0.0 is a bridge about to deny everything.
    """
    host, _, port = bind.rpartition(":")
    if not port.isdigit():
        return DEFAULT_URL
    if host in ("", "0.0.0.0", "::", "[::]", "*"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def control_plane_url() -> str:
    """Where the control plane is reachable from this machine.

    Derived from `HALYARD_BIND`, which is the one place the address is written
    down. Three keys that had to be kept in agreement by hand — the bind, the
    published Docker port, and a URL — is three chances to get it wrong, and
    getting it wrong denies every command with a message about a port.

    `HALYARD_URL` still overrides, for the case the derivation cannot cover: a
    control plane on another machine, reached over Tailscale or WireGuard.
    """
    explicit = lookup("HALYARD_URL", "")
    if explicit:
        return explicit
    bind = lookup("HALYARD_BIND", "")
    return _url_from_bind(bind) if bind else DEFAULT_URL


def timeout(key: str, default: float) -> float:
    try:
        return float(lookup(key, str(default)))
    except ValueError:
        return default
