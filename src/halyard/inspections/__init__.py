"""A project's own inspections, run over the last thing an agent said.

Some failures no guard catches. A claim made with no evidence behind it, a
change that quietly deletes or hides something, a gate reported green that
nobody ran: a rule matching words cannot tell any of these from the honest
version, and a model reading the text can. So a project writes each inspection
as a file — what to look for, and how to answer — and each one runs, on its
own, as a model turn over a reply. `/inspect` runs one by hand; a transition's
`inspect:` runs the ones it names before it goes.

The files belong to the project, for the reason `confirmation:` files do: what
is worth inspecting is something a team learns about its own failures. Halyard
reads them, adds what it can see for itself (`halyard.frame`), and hands the
answer back as it came.

**The turn stands in the project.** It runs in the project's directory, where
the code a report is about is: it searches and reads files and inspects diffs,
through native reading tools or read-only shell commands, and edits nothing.
Tests, scripts, database and network operations are forbidden. The runtime's
sandbox and the project's gate still apply: a read refused or unfinished leaves
that part of the inspection unmeasured rather than clean.

The prompt still says "check" where it speaks to the model, as it did before
these were called inspections: what a model is told is kept the same, so that
the answers before the name changed and after it can be compared.

**Every run is kept.** What the model was given, word for word, and what it
said, through `Keeper` into the table `record` writes — the one thing Halyard
keeps the text of, so an inspection can be compared and run again later.

**Two ports, and no chat.** An inspection reaches a model through `Asker` and
its record through `Keeper`, and knows nothing of Telegram, of any runtime or
of any tracker. `halyard.transitions` runs
inspections; nothing here hands anything on. `tests/test_layering.py` keeps it
that way.
"""

from halyard.inspections import record
from halyard.inspections.spec import Answer, Asker, Keeper, Kept, StoppedError
from halyard.inspections.turn import handed_on, prompt, run, unfenced

__all__ = [
    "Answer",
    "Asker",
    "Keeper",
    "Kept",
    "StoppedError",
    "handed_on",
    "prompt",
    "record",
    "run",
    "unfenced",
]
