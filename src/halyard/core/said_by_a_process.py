"""What to keep of a CLI's output when it fails.

The end of it. Every one of these tools prints a banner first — its version,
the model, the sandbox, the working directory — and puts what went wrong after
that. Keeping the first four hundred characters keeps the banner and throws
away the reason.

Measured on a real failure. Codex ran out of usage, the delivery failed, and
what reached the phone was:

    OpenAI Codex v0.145.0
    workdir: …/alpha-engine
    model: gpt-5.6-terra
    provider: openai
    approval: never
    sandbox: workspace-write …

Every word of that is true and none of it is the answer, which was that the
rate limit had been reached and would reset at 4:26 PM. It was in the output,
past the cut.

`commands/running.py` had already reached the same conclusion for a project's
own commands — "a test runner puts what broke at the end and one line of it is
a riddle" — and the runners had not. This is that decision, in the one place
both of them and the channel can share it.
"""

from __future__ import annotations

#: How many of the last lines to keep.
#:
#: Six: enough for a traceback's last frame and its exception, and short enough
#: that a banner cannot crowd out the line underneath it. Twelve was tried and
#: the whole banner fitted inside it.
#:
#: Filtering the banner out instead was the obvious next step and is not worth
#: it. Its lines look like `model:` and `sandbox:` — a rule that drops
#: `<word>: <value>` also drops `error: could not connect`, and dropping the
#: one line somebody needed is a worse failure than showing five they did not.
LINES = 6

#: A hard ceiling as well, because one line can be a whole JSON document.
CHARACTERS = 600


def the_useful_end(output: str | None, *, lines: int = LINES, limit: int = CHARACTERS) -> str:
    """The last few lines of what a process said, blank lines dropped.

    Trimmed from the end on both counts. Cutting a tail from the front would
    reintroduce the same fault one step further along: the last line is the one
    worth having, and it is the one a length cap takes first.
    """
    kept = [line.rstrip() for line in (output or "").splitlines() if line.strip()][-lines:]
    said = "\n".join(kept)
    return said[-limit:] if len(said) > limit else said
