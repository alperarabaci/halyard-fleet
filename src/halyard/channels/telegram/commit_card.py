"""The commit card, kept apart from the approval cards.

Same channel, different subject. An approval answers a question a session is
blocked on, carries a nonce and a deadline, and is owned by the approval store.
A commit answers nothing — it offers something — and is owned by
`halyard.commits`. Folding them into one module was the first shape, and it put
two unrelated lifecycles in one file for no gain but proximity.

What the two do share is Telegram's own limits, which is why the escaping,
the 64-byte `callback_data` cap and the message ceiling still come from
`cards`.
"""

from __future__ import annotations

import html

from halyard.channels.telegram.cards import CALLBACK_DATA_LIMIT, _fit
from halyard.commits import Uncommitted

#: Its own prefix, apart from approvals (`hf`) and preferences (`hc`).
#:
#: A commit proposal is answered once and must not be answerable twice, and it
#: still carries no nonce: what it answers is not a request the approval store
#: knows about. `Proposals.take` is the guard instead — see that module.
PREFIX = "hg"

MAKE = "m"
SEND = "s"
REWRITE = "w"
DROP = "x"
CONFIRM = "c"

#: How many changed files to name on the card. A phone shows about this many
#: without becoming a scroll, and the count above them is already the honest
#: summary — `render` says how many were left out.
FILES_SHOWN = 12


def callback_data(handle: str, action: str) -> str:
    """`hg:<handle>:m` — which proposal, and what to do with it."""
    data = f"{PREFIX}:{handle}:{action}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"commit callback exceeds the {CALLBACK_DATA_LIMIT}-byte limit: {data}")
    return data


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Decode a commit button into (handle, action), or None if it is not ours."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    _, handle, action = parts
    if action not in {MAKE, SEND, REWRITE, DROP, CONFIRM} or not handle:
        return None
    return handle, action


def _summary(work: Uncommitted) -> list[str]:
    """What is being committed, without the diff.

    Deliberately not the diff. Reading one on a phone is not a review — it is
    scrolling — and it would bury the only two things worth checking here: the
    branch and the message.

    New files get their own line. An edit to a tracked file is almost always
    the work; a file git has never seen might be the work, or might be
    something that wandered into the directory, and that is the one thing on
    this card worth a second look before tapping.
    """
    count = len(work.changes)
    lines = [
        f"Branch: <code>{html.escape(work.branch)}</code>",
        f"{count} file{'s' if count != 1 else ''}  +{work.insertions}/-{work.deletions}",
    ]
    if new := work.new_files:
        lines.append(f"{len(new)} of them new")
    shown = work.changes[:FILES_SHOWN]
    listed = "\n".join(f"{c.status}  {c.path}" for c in shown)
    if listed:
        lines += ["", f"<pre>{html.escape(listed)}</pre>"]
    if count > len(shown):
        lines.append(f"…and {count - len(shown)} more")
    return lines


def render(
    *,
    project: str,
    work: Uncommitted,
    message: str,
    summary: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> str:
    """The card: what changed, what it would be called, and what it would do.

    `summary` is the part a file list cannot give. Somebody away from their
    desk has not seen any of this code — the filenames say where an agent has
    been, and these lines say what it did there, which is the actual question
    being answered by tapping Commit.

    `warnings` go first, above even the heading. Telegram has no colour in
    message text — bold, italic, underline, strike, code and blockquote are the
    whole palette — so position carries the weight red would carry elsewhere: a
    blockquote draws its own rule down the left margin, and sitting before the
    heading means it is read before anything has been scrolled past.
    """
    said = [f"• {html.escape(line)}" for line in summary]
    alarm = (
        [
            "<blockquote>"
            + "\n".join(f"⚠️ <b>{html.escape(line)}</b>" for line in warnings)
            + "</blockquote>",
            "",
        ]
        if warnings
        else []
    )
    return _fit(
        [
            *alarm,
            f"<b>[COMMIT — {html.escape(project)}]</b>",
            "",
            *_summary(work),
            *(["", *said] if said else []),
            "",
            f"<pre>{html.escape(message)}</pre>",
        ]
    )


def render_resolved(*, project: str, message: str, outcome: str, by: str | None) -> str:
    """What the card becomes once it has been answered.

    Edited in place, like an approval, so scrolling back shows what happened
    rather than live-looking buttons on something already decided.
    """
    who = f" by {html.escape(by)}" if by else ""
    return _fit(
        [
            f"<b>{outcome}</b>{who}",
            "",
            f"Project: <code>{html.escape(project)}</code>",
            "",
            f"<pre>{html.escape(message)}</pre>",
        ]
    )


def keyboard(handle: str, *, confirmation: bool = False) -> dict:
    """The buttons under a commit card.

    The two that commit share the top row and the two that change nothing share
    the bottom one. That is the split worth keeping: a mistimed tap lands on
    something of the same kind. Commit next to Cancel would not be.

    Push has its own button rather than being what Commit does, because the two
    undo differently — a commit is a local thing to amend, and a push is a
    branch other people can already see.

    Confirmation sits between them on its own row, because it belongs to
    neither group: it commits nothing and it is not a dismissal either. It sends
    the project's round to the navigator and leaves the work exactly where it
    is. Shown whenever the project has a round to send — the model's flag says
    where to look, and whether to ask stays a person's call.
    """
    rows = [
        [
            {"text": "✅ Commit", "callback_data": callback_data(handle, MAKE)},
            {"text": "🚀 Commit & push", "callback_data": callback_data(handle, SEND)},
        ]
    ]
    if confirmation:
        rows.append(
            [{"text": "🔍 Confirmation round", "callback_data": callback_data(handle, CONFIRM)}]
        )
    rows.append(
        [
            {"text": "✏️ Rewrite", "callback_data": callback_data(handle, REWRITE)},
            {"text": "✖️ Cancel", "callback_data": callback_data(handle, DROP)},
        ]
    )
    return {"inline_keyboard": rows}
