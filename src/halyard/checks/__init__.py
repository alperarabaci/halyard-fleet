"""A project's own checks, run over the last thing an agent said.

Some failures no guard catches. A claim made with no evidence behind it, a
change that quietly deletes or hides something, a gate reported green that
nobody ran: a rule matching words cannot tell any of these from the honest
version, and a model reading the text can. So a project writes each check as a
file — what to look for, and how to answer — and each one runs, on its own, as
a model turn over a reply.

The files belong to the project, for the reason `confirmation:` files do: what
is worth checking is something a team learns about its own failures. Halyard
reads them, adds what it can see for itself (`halyard.frame`), and hands the
answer back as it came.

**The turn cannot look for itself.** It runs apart from the project, so it sees
only what it is given and can neither open a file nor run a command. The prompt
says so, which keeps a check that needed a command run from answering as though
it had run one.

**One port, and no chat.** A check reaches a model through `Asker` and knows
nothing of Telegram or of any runtime. `halyard.handoffs` runs checks; nothing
here hands anything on. `tests/test_layering.py` keeps it that way.
"""

from halyard.checks.spec import Answer, Asker
from halyard.checks.turn import handed_on, prompt, run, unfenced

__all__ = ["Answer", "Asker", "handed_on", "prompt", "run", "unfenced"]
