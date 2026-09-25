"""The account ZCode's engine is shown before it will run a turn for anybody.

The engine keeps no account of its own. Whoever drives it — the desktop, or
Halyard — pushes the provider catalog the application was built with, and until
that arrives the engine has no models to choose from and refuses a send with an
empty list.

**The catalog is the application's, read, not invented.** It sits under
`~/.zcode/v2/runtime/provider/**/zcode-builtin.json`, moves with every update,
and the newest one is the one the engine is running. Halyard copies the
providers out of it and marks the one it is about to use as current.

**The revision string is a hash of the file's path, not of the file.** Measured:
`zcode-builtin:<revision>:<sha256 of the resolved path>` is what the engine
computes for itself, and anything else is accepted with a "received" and then
materialises no models at all — a silent failure that costs a turn. The path is
why this is read from disk rather than carried in configuration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the application keeps the catalogs it ships, one per version it has run.
RUNTIME = Path(".zcode/v2/runtime/provider")

#: What one is called, wherever it sits under that root.
CATALOG = "zcode-builtin.json"

#: How the engine says which catalog a snapshot was built from.
REVISION = "zcode-builtin:{revision}:{digest}"


@dataclass(frozen=True)
class Account:
    """A catalog read from disk, as the engine wants to be told about it."""

    #: The file it came from, resolved — the string that is hashed.
    catalog: Path
    #: What the catalog calls itself.
    revision: str
    #: The models each provider offers, by provider id.
    providers: dict[str, list[str]]
    #: The provider a turn will run on.
    current: str

    def snapshot(self, now: float | None = None) -> dict:
        """What `provider/updateAccountConfig` takes.

        `access` says `zhipu-account` for every provider because that is the
        only type the engine's own schema accepts — measured; a coding-plan key
        is refused there and travels in the auth answer instead, which the
        provider accepts.
        """
        digest = hashlib.sha256(str(self.catalog).encode("utf-8")).hexdigest()
        return {
            "revision": f"halyard-{int(now or time.time())}",
            "basedOnZCodeBuiltinRevision": REVISION.format(revision=self.revision, digest=digest),
            "providers": {
                name: {
                    "builtinModelIds": models,
                    "access": {"type": "zhipu-account", "entitled": True},
                }
                for name, models in self.providers.items()
            },
            "states": {
                name: {
                    "availability": "available",
                    "entitled": True,
                    "current": name == self.current,
                }
                for name in self.providers
            },
        }


def newest(home: Path | None = None) -> Path | None:
    """The catalog of the version the application is running now.

    By modification time rather than by version number: the directories are
    dotted strings — `3.12.3` — and sorting those as numbers is how an earlier
    attempt turned a version into `NaN` and read the wrong file.
    """
    root = (home or Path.home()) / RUNTIME
    try:
        found = sorted(root.rglob(CATALOG), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError as unreadable:
        logger.info("No ZCode provider catalog under %s: %s", root, unreadable)
        return None
    return found[0].resolve() if found else None


def read(current: str, home: Path | None = None) -> Account | None:
    """The newest catalog, with `current` marked as the provider to run on.

    None when there is no catalog to read or it is not the shape this knows —
    the caller says so and delivers nothing, rather than pushing a snapshot the
    engine will accept and quietly ignore.
    """
    catalog = newest(home)
    if catalog is None:
        return None
    try:
        document = json.loads(catalog.read_text(encoding="utf-8"))
        rules = document["config"]["providerConfigRules"]["providerRules"]
    except (OSError, ValueError, KeyError, TypeError) as unreadable:
        logger.warning("ZCode's provider catalog %s could not be read: %s", catalog, unreadable)
        return None
    providers: dict[str, list[str]] = {
        str(rule.get("providerId")): list((rule.get("config") or {}).get("builtinModelIds") or [])
        for rule in rules
        if isinstance(rule, dict) and rule.get("providerId")
    }
    if not providers:
        logger.warning("ZCode's provider catalog %s lists no providers", catalog)
        return None
    return Account(
        catalog=catalog,
        revision=str(document.get("revision", "")),
        providers=providers,
        current=current,
    )
