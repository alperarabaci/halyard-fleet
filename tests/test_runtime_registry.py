"""The tripwire for the runtime registry.

Discovery without one is a net negative, and this project has the incident to
prove the general point: a Codex trust key built as `pretooluse` where Codex
writes `pre_tool_use` matched nothing, so every hook read as untrusted and
`doctor` reported no gate on a project that had one. Nothing failed. The
checker was simply confidently wrong, and the obvious response to its FAIL was
to go and re-grant trust that had never been missing.

`registry.discover()` fails the same way by construction: a package whose
`RUNTIME` is misspelled, mistyped, or lost in a bad merge does not raise, it
disappears — and a runtime that has disappeared has no gate, which is exactly
the shape of failure the rest of this repository is built to avoid.

So membership is asserted here explicitly. Adding a runtime is meant to be
dropping in a package; making that package *count* is meant to be a visible
edit to this file, seen in review.
"""

from __future__ import annotations

import pytest

from halyard.agents import registry
from halyard.agents.spec import RuntimeSpec

#: Every runtime this build ships. Growing this set is the point of the edit.
EXPECTED = {"claude-code", "codex", "antigravity"}


def test_exactly_the_expected_runtimes_are_found() -> None:
    """Both directions on purpose.

    A missing one is a runtime with no gate. An unexpected one is a package
    that started gating projects without anybody deciding it should.
    """
    assert set(registry.discover()) == EXPECTED


def test_every_runtime_is_keyed_by_its_own_name() -> None:
    """The key is what a seat's `runtime:` says, and a mismatch would make a
    seat unroutable while the package looks perfectly present."""
    for key, spec in registry.discover().items():
        assert key == spec.name


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_runtime_describes_everything_core_would_otherwise_guess(name: str) -> None:
    """The fields exist because a module could not do its job without them.

    Each was a branch on a runtime name in `halyard/` before this registry, and
    a spec that left one unset would send that module back to guessing.
    """
    spec = registry.discover()[name]

    assert isinstance(spec, RuntimeSpec)
    assert spec.human, "doctor and the wizard both print this"
    assert spec.binary, "installed() has to have something to look for"
    assert spec.hooks.settings, "wiring has to know which file to merge into"
    assert spec.hooks.matcher, "a hook with no matcher gates nothing"
    assert spec.hooks.dialect in {"wrapped", "named"}
    assert callable(spec.runner)
    assert callable(spec.find_session)
    assert callable(spec.list_sessions)


def test_the_order_is_stable() -> None:
    """Iteration order decides which runtime `halyard init` offers first and
    which seat a fallback lands on. Neither should depend on the order a
    filesystem happens to return directories in."""
    assert list(registry.discover()) == list(registry.discover(refresh=True))


def test_the_names_are_what_a_seat_may_be_configured_as() -> None:
    """One source of truth. A runtime the registry knows and `Seat` refuses is
    a package that installed itself and can never be used."""
    from halyard.core.seats import known_runtimes

    assert set(known_runtimes()) == EXPECTED
