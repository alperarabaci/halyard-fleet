"""A workflow: a project's handoffs, taken in the order it wrote them down.

The rung above `halyard.handoffs`, the way a handoff is the rung above a
command. A handoff hands one reply on and stops there; somebody at a phone
reads what came back and presses the next one. A workflow presses it: each step
is one of the project's handoffs going to one of its seats, and the reply's own
last line says whether the work goes on, comes back a step, or waits for a
person.

**The words that decide are fixed:** `DECISION: forward`, `back` or `wait` —
see `decisions`. Each step's envelope says where each of them goes, in lines
the workflow adds itself (`envelope`), so a handoff pressed by hand is the same
handoff with none of them.

**It stops where it must, and nowhere else.** No step asks permission to go: a
flow that needed a tap between every pair of seats would be the list of buttons
it replaced. It stops when a step would go round more often than its project
allowed it to, when a seat says to wait, and when a message reached nobody —
and each of those is said in the chat it was started from, with the run kept so
it can go on afterwards.

**Nothing here knows a chat or a runtime.** A step is delivered by whatever
drives it — the Telegram channel today — through `halyard.handoffs`, which is
the one place that knows how a reply reaches a seat. `tests/test_layering.py`
keeps it that way.
"""

from halyard.workflows.decisions import Decision, read
from halyard.workflows.envelope import lines_for
from halyard.workflows.flow import Next, after
from halyard.workflows.runs import Run, clear, current, save

__all__ = [
    "Decision",
    "Next",
    "Run",
    "after",
    "clear",
    "current",
    "lines_for",
    "read",
    "save",
]
