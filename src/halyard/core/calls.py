"""The tool calls one turn of Halyard's own made, as its runtime reported them.

A turn's answer says what the model concluded. This says what it did to get
there: each call, what it was asked to read, and how it came back — taken from
the runtime's own event stream, never from what the model says it did. An
inspection's trace is built from it, beside the gate's record of the same turn.

A runtime that can say this fills it in while the turn runs, so whatever came
before a turn that ran out of time or was stopped is still here. Measured on
2026-10-25: Claude Code 2.1.303 reports every `tool_use` and its `tool_result`
in `--output-format stream-json --verbose`; Codex 0.160.0 reports each
`command_execution` item in `codex exec --json`, and nothing at all for a
command the gate denied. A runtime that cannot leaves it empty and
`reported` False.

What a call read is kept — a path, a pattern, a command — redacted as a card
is, and never what came back: the contents of a file are the project's, not a
record's.
"""

from __future__ import annotations

from dataclasses import dataclass

from halyard.core.redaction import Redactor

#: It ran and came back.
RAN = "ran"
#: It ran, and its tool said it failed: a file that is not there, a command
#: that exited with something other than 0. It ran all the same.
ERROR = "error"
#: It was refused before it ran — by the gate, through the runtime's hook.
DENIED = "denied"

_REDACTOR = Redactor()


@dataclass
class Call:
    """One tool call, as the runtime reported it."""

    #: The runtime's own id for the call. Claude Code's hook carries the same
    #: one as `tool_use_id`; Codex's does not.
    id: str
    #: `Read`, `Grep`, `Glob` or `Bash` — a Codex command is `Bash` too, as
    #: its hook calls it.
    tool: str
    #: What it read: a path, a pattern or a command.
    what: str
    #: `RAN`, `ERROR` or `DENIED` once it came back; None until then.
    came: str | None = None
    #: An exit code other than 0, or why it was refused, in a few words.
    detail: str = ""


class Calls:
    """The calls one turn made, in the order it made them."""

    def __init__(self) -> None:
        #: Whether the runtime reports its calls at all. Set by the runtime
        #: that fills this in: when it stays False, only the gate knows what
        #: the turn read, and only the reads that met it.
        self.reported = False
        self._calls: dict[str, Call] = {}

    def asked(self, ident: str, tool: str, what: str) -> None:
        """A call made. Said again, it is the same call."""
        if ident not in self._calls:
            self._calls[ident] = Call(ident, tool, _REDACTOR.redact(what).text)

    def came_back(
        self, ident: str, came: str, detail: str = "", *, tool: str = "", what: str = ""
    ) -> None:
        """How a call came back. One the runtime did not report making is
        made here, with what it says now."""
        self.asked(ident, tool or "?", what)
        call = self._calls[ident]
        call.came = came
        call.detail = detail

    def __iter__(self):
        return iter(list(self._calls.values()))

    def __len__(self) -> int:
        return len(self._calls)
