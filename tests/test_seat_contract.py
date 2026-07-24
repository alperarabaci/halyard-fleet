"""The contract every source of seats has to satisfy.

Seats have one producer today — `from_environment` — and are about to have a
second. That is the shape both Codex postmortems warn about: a boundary that
can *parse* is not the same as a boundary that produces something the rest of
the system already trusts, and when a new producer lands beside a restructuring
nothing can tell a producer defect from a routing defect.

So the contract lives here, parametrised over producers, and the env producer
satisfies it now. A YAML producer is added as a second parameter and inherits
every assertion below without anyone deciding which of them still apply.

Each case is a configuration and the seats it must yield. The producer's job is
to turn its own dialect into that list; everything downstream — routing,
`doctor`, the wizard's defaults — is written against the list and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from halyard.core.events import Role
from halyard.core.seats import Seat, from_environment

#: A producer takes whatever it reads and returns seats.
Producer = Callable[[dict], list[Seat]]

#: Every case, as (name, env-dialect configuration, expected seats).
#:
#: Expressed in the env dialect because that is the producer that exists. A
#: second producer supplies its own translation of the same cases rather than
#: its own cases — the point is that two dialects mean the same thing, which is
#: not demonstrated by two suites that never compare.
CASES: list[tuple[str, dict, list[Seat]]] = [
    (
        "four seats across two runtimes",
        {
            "HALYARD_SEATS": "nav,drv,xnav,xdrv",
            "HALYARD_SEAT_NAV": "runtime=claude-code session=a-nav chat=-1001 role=navigator",
            "HALYARD_SEAT_DRV": "runtime=claude-code session=a-drv chat=-1002 role=driver",
            "HALYARD_SEAT_XNAV": "runtime=codex session=x-nav chat=-1003 role=navigator",
            "HALYARD_SEAT_XDRV": "runtime=codex session=x-drv chat=-1004 role=driver",
        },
        [
            Seat("nav", "claude-code", "a-nav", "-1001", Role.NAVIGATOR),
            Seat("drv", "claude-code", "a-drv", "-1002", Role.DRIVER),
            Seat("xnav", "codex", "x-nav", "-1003", Role.NAVIGATOR),
            Seat("xdrv", "codex", "x-drv", "-1004", Role.DRIVER),
        ],
    ),
    (
        "a forum topic is a destination of its own",
        {
            "HALYARD_SEATS": "drv",
            "HALYARD_SEAT_DRV": "runtime=codex session=s chat=-1004:12 role=driver",
        },
        [Seat("drv", "codex", "s", "-1004:12", Role.DRIVER)],
    ),
    (
        "a seat with nowhere of its own to speak",
        {"HALYARD_SEATS": "spare", "HALYARD_SEAT_SPARE": "runtime=codex session=s"},
        [Seat("spare", "codex", "s", None, None)],
    ),
    (
        "two seats sharing a role, told apart by runtime and session",
        {
            "HALYARD_SEATS": "drv,xdrv",
            "HALYARD_SEAT_DRV": "runtime=claude-code session=a chat=-1 role=driver",
            "HALYARD_SEAT_XDRV": "runtime=codex session=b chat=-2 role=driver",
        },
        [
            Seat("drv", "claude-code", "a", "-1", Role.DRIVER),
            Seat("xdrv", "codex", "b", "-2", Role.DRIVER),
        ],
    ),
    (
        "a label with a dash",
        {"HALYARD_SEATS": "codex-drv", "HALYARD_SEAT_CODEX_DRV": "runtime=codex session=s"},
        [Seat("codex-drv", "codex", "s", None, None)],
    ),
    ("nothing configured", {}, []),
]

#: Configurations that must be refused rather than half-understood. Silence
#: here leaves a seat somebody believes in and cannot reach.
REFUSALS: list[tuple[str, dict, str]] = [
    (
        "an unknown runtime",
        {"HALYARD_SEATS": "x", "HALYARD_SEAT_X": "runtime=gpt session=s"},
        "Use one of",
    ),
    ("a seat named but never described", {"HALYARD_SEATS": "ghost"}, "is not set"),
    (
        "a mistyped field",
        {"HALYARD_SEATS": "x", "HALYARD_SEAT_X": "runtime=codex sesion=typo"},
        "unknown field",
    ),
]

#: Every producer, by name. Add YAML here and it inherits the whole contract.
PRODUCERS: dict[str, Producer] = {"env": from_environment}


@pytest.mark.parametrize("producer_name", list(PRODUCERS))
@pytest.mark.parametrize(("case_name", "config", "expected"), CASES, ids=[c[0] for c in CASES])
def test_a_producer_yields_the_agreed_seats(
    producer_name: str, case_name: str, config: dict, expected: list[Seat]
) -> None:
    """Same configuration, same seats, whichever dialect expressed it."""
    produced = PRODUCERS[producer_name](config)

    assert produced == expected, f"{producer_name} disagreed on: {case_name}"


@pytest.mark.parametrize("producer_name", list(PRODUCERS))
@pytest.mark.parametrize(("case_name", "config", "message"), REFUSALS, ids=[c[0] for c in REFUSALS])
def test_a_producer_refuses_what_it_cannot_honour(
    producer_name: str, case_name: str, config: dict, message: str
) -> None:
    """A configuration that cannot be honoured must fail loudly.

    Every alternative is worse: a seat silently dropped is one somebody
    believes exists and cannot reach, and a seat silently altered routes work
    somewhere nobody chose.
    """
    with pytest.raises(ValueError, match=message):
        PRODUCERS[producer_name](config)


@pytest.mark.parametrize("producer_name", list(PRODUCERS))
def test_a_session_address_is_never_a_bare_id(producer_name: str) -> None:
    """From the Codex postmortem, stated as a rule: treat a session address as
    `(runtime, session_id)`, never as a bare session ID.

    Two seats can hold the same session name under different runtimes, and a
    producer that dropped the runtime would make them indistinguishable — which
    is the defect that sent a Codex session id to the Claude Code runner.
    """
    produced = PRODUCERS[producer_name](
        {
            "HALYARD_SEATS": "a,b",
            "HALYARD_SEAT_A": "runtime=claude-code session=same-name chat=-1",
            "HALYARD_SEAT_B": "runtime=codex session=same-name chat=-2",
        }
    )

    assert [seat.runtime for seat in produced] == ["claude-code", "codex"]
    assert len({(seat.runtime, seat.session) for seat in produced}) == 2
