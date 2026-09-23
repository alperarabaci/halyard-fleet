"""A workflow: a project's handoffs, taken in the order it wrote them down.

The rung above `halyard.handoffs`, the way a handoff is the rung above a
command. A handoff hands one reply on and stops there; somebody at a phone
reads what came back and presses the next one. A workflow presses it: each step
is one of the project's handoffs going to one of its seats, and the reply's own
last line says whether the work goes on, comes back a step, or waits for a
person.

**The words that decide are `forward`, `back`, `wait` and `next`** unless a
project names its own — see `decisions`. Each step's envelope says where each
of them goes, in lines the workflow adds itself (`envelope`), so a handoff
pressed by hand is the same handoff with none of them.

**A flow can go round in phases.** One list inside the flow is the stretch of
steps a plan in parts goes through once per part; its last step says `next`
for another phase or `forward` to leave them — see `flow`.

**It stops where it must, and nowhere else.** No step asks permission to go: a
flow that needed a tap between every pair of seats would be the list of buttons
it replaced. It stops when a step would go round more often than its project
allowed it to, when the phases would, when a phase ends undecided, when a seat
says to wait, and when a message reached nobody — and each of those is said in
the chat it was started from, with the run kept so it can go on afterwards.

**Nothing here knows a chat or a runtime.** A step is delivered by whatever
drives it — the Telegram channel today — through `halyard.handoffs`, which is
the one place that knows how a reply reaches a seat. `tests/test_layering.py`
keeps it that way.
"""

from halyard.workflows.decisions import Decision, decided, read, word_for
from halyard.workflows.envelope import lines_for
from halyard.workflows.flow import PHASES, Next, after
from halyard.workflows.runs import Round, Run, clear, counted_as, current, record, save

__all__ = [
    "PHASES",
    "Decision",
    "Next",
    "Round",
    "Run",
    "after",
    "clear",
    "counted_as",
    "current",
    "decided",
    "lines_for",
    "read",
    "record",
    "save",
    "word_for",
]
