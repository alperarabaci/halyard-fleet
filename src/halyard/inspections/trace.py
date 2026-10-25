"""What an inspection read, from the records — never from the model's words.

An inspection can answer without a read it asked for, and say nothing of it:
a Codex model goes on while a card is still open. Whether an answer rests on
everything it asked to read has to be decided from what was recorded, so each
turn gets a trace — every read it asked for, once, with what became of it —
and from the trace a status: complete, incomplete, or unmeasured when there is
no answer at all.

Two records of one turn, each holding half of it:

- **The runtime's stream** says what the turn did. Claude Code reports every
  tool call and how it came back, a read its hook refused among them; Codex
  reports each command it ran, with its exit code, and nothing at all for one
  the hook denied or one still waiting on it. See `halyard.core.calls`.
- **The gate** says what it made of each call that met it: a card and its
  answer — allowed or denied and by whom, expired, or still open when the
  turn ended — or a call let through or refused with nobody asked.

Where both name a call, by the id Claude Code's hook carries as well, they are
one read: what the stream says, with the gate's word on why it never ran when
it did not. A read the gate stopped and the stream never mentions is added from
the gate. A read the gate let through is the stream's to report — added from
the gate only when the runtime reports nothing, as opencode does.

A read that ran and failed, a `grep` that found nothing and exited 1, ran.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from halyard.core import calls as stream
from halyard.inspections.spec import Answer, Decided, Outcome, Read, Status, Trace

_CAME = {stream.RAN: Outcome.RAN, stream.ERROR: Outcome.ERROR, stream.DENIED: Outcome.DENIED}


def merged(calls: Iterable[stream.Call], gate: Sequence[Decided], *, reported: bool) -> Trace:
    """One trace of a turn, from what its runtime `reported` it called and
    what the `gate` made of each call that met it. See the module."""
    by_ref = {decided.ref: decided for decided in gate if decided.ref}
    matched: set[str] = set()
    reads: list[Read] = []
    for call in calls:
        decided = by_ref.get(call.id) if call.id else None
        if decided is not None:
            matched.add(call.id)
        reads.append(_read(call, decided))
    for decided in gate:
        if decided.ref in matched:
            continue
        if decided.stopped is not None:
            reads.append(Read(decided.tool, decided.what, decided.stopped, decided.detail))
        elif not reported:
            reads.append(Read(decided.tool, decided.what, Outcome.ALLOWED, decided.detail))
    return Trace(tuple(reads), reported)


def _read(call: stream.Call, decided: Decided | None) -> Read:
    """One call the runtime reported, with what the gate made of it."""
    if decided is not None and decided.stopped is not None:
        return Read(call.tool, call.what, decided.stopped, decided.detail)
    if call.came is None:
        return Read(
            call.tool,
            call.what,
            Outcome.UNFINISHED,
            "started, and had not come back when the inspection ended",
        )
    outcome = _CAME.get(call.came, Outcome.RAN)
    if outcome is Outcome.DENIED:
        return Read(call.tool, call.what, outcome, call.detail or "denied by the hook")
    detail = ", ".join(part for part in (decided.detail if decided else "", call.detail) if part)
    return Read(call.tool, call.what, outcome, detail)


def reads_said(trace: Trace) -> str:
    """`12 reads` — or `2 reads seen at the gate`, when the runtime did not
    say what the turn read and only the gate's are known."""
    count = len(trace.reads)
    return f"{count} read{'s' if count != 1 else ''}" + (
        "" if trace.reported else " seen at the gate"
    )


def summed_up(status: Status, trace: Trace, why: str = "") -> str:
    """How an inspection came out, in a few words: `complete · 12 reads`,
    `incomplete · 12 reads, 1 did not run`, `unmeasured — <why>` — or just
    `unmeasured` when no `why` is given."""
    if status is Status.UNMEASURED:
        return f"unmeasured — {why}" if why else "unmeasured"
    if status is Status.INCOMPLETE:
        return f"incomplete · {reads_said(trace)}, {len(trace.not_run)} did not run"
    return f"complete · {reads_said(trace)}"


def said(answer: Answer, *, why: bool = True) -> str:
    """`summed_up` for an answer — with why it is unmeasured, unless `why` is
    False because the answer says so under it."""
    return summed_up(answer.status, answer.trace, answer.why if why else "")


def to_json(trace: Trace) -> str:
    """A trace as the kept record holds it."""
    return json.dumps(
        {
            "reported": trace.reported,
            "reads": [
                {"tool": r.tool, "what": r.what, "outcome": r.outcome.value, "detail": r.detail}
                for r in trace.reads
            ],
        },
        ensure_ascii=False,
    )


def from_json(text: str | None) -> Trace | None:
    """A kept trace read back — None for a run kept without one, or one that
    cannot be read. A read whose outcome this version does not know is left
    out rather than guessed at."""
    if not text:
        return None
    try:
        kept = json.loads(text)
        listed = kept.get("reads") or []
    except (ValueError, AttributeError):
        return None
    reads = []
    for read in listed:
        try:
            reads.append(
                Read(
                    str(read["tool"]),
                    str(read["what"]),
                    Outcome(read["outcome"]),
                    str(read.get("detail") or ""),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return Trace(tuple(reads), bool(kept.get("reported")))
