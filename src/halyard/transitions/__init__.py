"""Taking the work to its next stage, the way a project defines it: a seat's
reply, carried to the seat that works on it next. Called handoffs until
2026-09-24; `docs/transitions.md` says why the words are these.

A navigator writes a prompt; the reviewer is handed it with the project's
review text in front; the navigator gets the review back; the driver gets the
prompt; and the driver's discovery report goes back to the navigator with the
project's inspections already run over it. Each of those is a transition: a reply, the
project's own words for whoever receives it, and — for the one that matters
most — the answers of the inspections it names, so the navigator does not have to
find every missing proof alone.

**Commands and inspections are used, never owned.** A transition runs a
project's commands through `Runner` and its inspections through
`halyard.inspections`, in that order, and hands each what came before it: an
inspection reads in its envelope what the commands did. None of them knows a
chat or a runtime — a model is reached through `inspections.Asker`, a command
through `Runner` and a seat through `Delivery` — and the Telegram channel is the
one adapter answering them today.
`tests/test_layering.py` keeps it that way.

**Work goes round.** Inside a workflow, a reply sent back comes round again,
and the step counts it — see `halyard.transitions.rounds`. The count goes on the
envelope, and a transition with a `followup_prompt:` sends that from the second
round on, with what the seat said back to the round before.
"""

from halyard.transitions.flow import take
from halyard.transitions.message import compose
from halyard.transitions.spec import Delivery, Handed, Previous, Runner

__all__ = ["Delivery", "Handed", "Previous", "Runner", "compose", "take"]
