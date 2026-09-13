"""Handing a reply from one seat to another, the way a project defines it.

A navigator writes a prompt; the reviewer is handed it with the project's
review text in front; the navigator gets the review back; the driver gets the
prompt; and the driver's discovery report goes back to the navigator with the
project's checks already run over it. Each of those is a handoff: a reply, the
project's own words for whoever receives it, and — for the one that matters
most — the answers of the checks it names, so the navigator does not have to
find every missing proof alone.

**Checks are used, never owned.** A handoff runs checks through
`halyard.checks`; a check never hands anything on. Neither knows a chat or a
runtime: a model is reached through `checks.Asker` and a seat through
`Delivery`, and the Telegram channel is the one adapter answering both today.
`tests/test_layering.py` keeps it that way.
"""

from halyard.handoffs.flow import hand_off
from halyard.handoffs.message import compose
from halyard.handoffs.spec import Delivery, Handed

__all__ = ["Delivery", "Handed", "compose", "hand_off"]
