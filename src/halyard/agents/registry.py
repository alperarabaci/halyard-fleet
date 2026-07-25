"""Every runtime this build knows, found rather than listed.

A package under `halyard.agents` that exposes `RUNTIME` is a runtime. Nothing
else registers it: no tuple to append to, no import to add, no dispatch table
that four modules each keep their own copy of.

**This is the pattern's cost, taken deliberately.** Membership is no longer one
grep-able list, `find usages` gets weaker, and a package whose `RUNTIME` is
misspelled disappears without a word instead of failing to import. Against
that: adding a runtime here used to mean finding some sixty branches spread
across eight modules that are otherwise about something else, and the three
that were missed each produced a gate that reported itself installed and never
fired. A quieter failure that is caught by a test beats a louder one nobody
runs into until the gate is already off.

So the tripwire is not optional and ships with this file:
`tests/test_runtime_registry.py` names the members it expects, and growing the
set is a visible edit in review rather than something that happens by itself.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from halyard.agents.spec import RuntimeSpec

logger = logging.getLogger(__name__)

#: What a package has to call its descriptor to be found.
ATTRIBUTE = "RUNTIME"

#: What a seat is when nobody said. Every configuration dialect this project
#: has ever had defaulted to Claude Code, and changing that would silently
#: re-point seats written before the default moved.
DEFAULT = "claude-code"

_cache: dict[str, RuntimeSpec] | None = None


def discover(*, refresh: bool = False) -> dict[str, RuntimeSpec]:
    """Every runtime package, by name, in a stable order.

    Sorted by module name rather than by whatever order the filesystem hands
    back. Iteration order decides which seat a fallback picks and which runtime
    is offered first by `halyard init`, and neither should depend on the
    machine it is running on.

    Cached: this imports every runtime package, and it is asked on every
    `doctor` line and every wire. `refresh=True` is for tests that add one.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    import halyard.agents as package

    found: dict[str, RuntimeSpec] = {}
    for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        try:
            imported = importlib.import_module(f"{package.__name__}.{module.name}")
        except Exception:
            # One runtime package that cannot be imported must not take the
            # other two down with it — a machine missing an optional
            # dependency should lose that runtime, not the control plane.
            logger.exception("Could not load the runtime package %r", module.name)
            continue
        spec = getattr(imported, ATTRIBUTE, None)
        if isinstance(spec, RuntimeSpec):
            found[spec.name] = spec

    _cache = found
    return found


def get(name: str) -> RuntimeSpec | None:
    return discover().get(name)


def names() -> tuple[str, ...]:
    """Every runtime name, for validating configuration and for messages."""
    return tuple(discover())


def installed() -> tuple[RuntimeSpec, ...]:
    """The runtimes actually on this machine.

    Wiring is offered for these and removal is attempted for all of them. The
    asymmetry is deliberate: adding a gate for a runtime nobody has is clutter,
    while leaving one behind after a CLI is uninstalled is a hook pointing at a
    bridge nothing will ever call.
    """
    return tuple(spec for spec in discover().values() if spec.on_this_machine())
