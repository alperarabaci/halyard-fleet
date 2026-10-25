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
that part of the inspection unmeasured rather than clean. A read that waits for
a person's approval may never run — the card denied, expired, or still open
when the turn ends — and a model may answer without it, so Halyard says which
reads did not run under the answer, in its own words (`not_run_block`).

**Every answer has a status.** What the turn read is traced from what the
runtime reported and what the gate decided, never from what the model says it
read (`trace`): complete when every read it asked for ran, incomplete when one
did not, unmeasured when there is no answer. An incomplete answer is marked,
and whatever runs it goes on.

The prompt said "check" where it spoke to the model until 2026-10-24, so that
answers from either side of the rename could be compared; it says "inspection"
since. A kept run holds the words it was given, and a repeat gives them again.

**Every run is kept.** What the model was given, word for word, and what it
said, through `Keeper` into the table `record` writes — the one thing Halyard
keeps the text of, so an inspection can be compared and run again later.

**Two ports, and no chat.** An inspection reaches a model through `Asker` and
its record through `Keeper`, and knows nothing of Telegram, of any runtime or
of any tracker. `halyard.transitions` runs
inspections; nothing here hands anything on. `tests/test_layering.py` keeps it
that way.
"""

from halyard.inspections import record, trace
from halyard.inspections.spec import (
    Answer,
    Asker,
    Decided,
    Gated,
    Keeper,
    Kept,
    NotRun,
    Outcome,
    Read,
    Status,
    StoppedError,
    Trace,
)
from halyard.inspections.trace import merged, said
from halyard.inspections.turn import handed_on, not_run_block, prompt, run, unfenced

__all__ = [
    "Answer",
    "Asker",
    "Decided",
    "Gated",
    "Keeper",
    "Kept",
    "NotRun",
    "Outcome",
    "Read",
    "Status",
    "StoppedError",
    "Trace",
    "handed_on",
    "merged",
    "not_run_block",
    "prompt",
    "record",
    "run",
    "said",
    "trace",
    "unfenced",
]
