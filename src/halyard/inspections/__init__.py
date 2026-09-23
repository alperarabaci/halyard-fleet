"""A project's own inspections, run over the last thing an agent said.

Some failures no guard catches. A claim made with no evidence behind it, a
change that quietly deletes or hides something, a gate reported green that
nobody ran: a rule matching words cannot tell any of these from the honest
version, and a model reading the text can. So a project writes each inspection
as a file — what to look for, and how to answer — and each one runs, on its
own, as a model turn over a reply. `/inspect` runs one by hand; a handoff's
`inspect:` runs the ones it names before it goes.

The files belong to the project, for the reason `confirmation:` files do: what
is worth inspecting is something a team learns about its own failures. Halyard
reads them, adds what it can see for itself (`halyard.frame`), and hands the
answer back as it came.

**The turn stands in the project.** It runs in the project's directory, where
the code a report is about is: it reads what the inspection needs, runs only
the commands its text names — each put in front of a person by the project's
gate first — and edits nothing. The prompt says so, and that a command refused
or unfinished leaves the inspection unmeasured rather than clean. Whoever is
asked to allow one of those commands can stop the inspection instead.

The prompt still says "check" where it speaks to the model, as it did before
these were called inspections: what a model is told is kept the same, so that
the answers before the name changed and after it can be compared.

**A finding can label the task.** A project writes under `label_findings:` what
its inspections' answers say when they found something, in its own words, and
an answer that says one of them puts `halyard:<inspection>` on the task. The
inspection decides, from the project's settings — whoever runs it has no say —
so one in a handoff labels exactly as one run by hand. Halyard only adds.

**Two ports, and no chat.** An inspection reaches a model through `Asker` and
the task through `Labeller`, and knows nothing of Telegram, of any runtime or
of any tracker. `halyard.handoffs` runs inspections; nothing here hands anything
on. `tests/test_layering.py` keeps it that way.
"""

from halyard.inspections.spec import Answer, Asker, Labeller, StoppedError
from halyard.inspections.turn import finding, handed_on, prompt, run, unfenced

__all__ = [
    "Answer",
    "Asker",
    "Labeller",
    "StoppedError",
    "finding",
    "handed_on",
    "prompt",
    "run",
    "unfenced",
]
