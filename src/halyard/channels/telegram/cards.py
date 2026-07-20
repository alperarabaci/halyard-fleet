"""Turning an approval into something you can act on from a phone.

The card has one job: let someone decide correctly in the few seconds they will
actually give it. That means the command is visible, the risk is visible, and
the deadline is visible — and nothing else competes with them.

**`callback_data` is capped at 64 bytes by Telegram.** A request id and a nonce
together do not fit with room to spare, so the button carries a short handle
instead of the full id and the adapter resolves it. The nonce still travels in
full and is still what the store checks; shortening *that* would be shortening
the only secret involved.
"""

from __future__ import annotations

import html
from datetime import datetime

from halyard.core.approvals import ApprovalRequest
from halyard.core.events import RiskLevel

#: Telegram's hard limit on callback_data.
CALLBACK_DATA_LIMIT = 64

#: How much of a request id the button carries. Enough to be unambiguous among
#: the handful of approvals that can be open at once, short enough to leave the
#: nonce room.
HANDLE_LENGTH = 12

PREFIX = "hf"

ALLOW = "a"
DENY = "d"
SHOW_FULL = "f"

_RISK_BADGE = {
    RiskLevel.LOW: "🟢 LOW",
    RiskLevel.MEDIUM: "⚠️ MEDIUM",
    RiskLevel.HIGH: "🛑 HIGH",
}

#: Telegram's message length limit.
MESSAGE_LIMIT = 4096


def handle_of(request: ApprovalRequest) -> str:
    """The short id a button carries."""
    return request.request_id.removeprefix("req_")[:HANDLE_LENGTH]


def callback_data(request: ApprovalRequest, action: str) -> str:
    """Encode a button press.

    Raises if the result would exceed Telegram's limit, rather than letting the
    API reject the card at send time — which would mean an approval nobody ever
    sees, on the exact path where nobody seeing it is the failure.
    """
    data = f"{PREFIX}:{handle_of(request)}:{request.nonce}:{action}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(
            f"callback_data is {len(data.encode('utf-8'))} bytes, over Telegram's "
            f"{CALLBACK_DATA_LIMIT}-byte limit: {data[:16]}…"
        )
    return data


def parse_callback_data(data: str) -> tuple[str, str, str] | None:
    """Decode a button press into (handle, nonce, action), or None if it is not ours."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    _, handle, nonce, action = parts
    if action not in {ALLOW, DENY, SHOW_FULL} or not handle or not nonce:
        return None
    return handle, nonce, action


def format_remaining(expires_at: datetime, now: datetime) -> str:
    seconds = int((expires_at - now).total_seconds())
    if seconds <= 0:
        return "expired"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def render(request: ApprovalRequest, *, now: datetime) -> str:
    """The approval card."""
    role = (request.role.value if request.role else "agent").upper()
    lines = [
        f"<b>[{role} — PERMISSION REQUEST]</b>  {_RISK_BADGE[request.risk]}",
        "",
        f"Project: <code>{html.escape(request.project)}</code>",
        f"Session: <code>{html.escape(_short_session(request.session_id))}</code>",
        f"Tool: <code>{html.escape(request.tool)}</code>",
        "",
        f"<pre>{html.escape(request.command_summary)}</pre>",
    ]
    if request.reason:
        lines += ["", f"Why: {html.escape(request.reason)}"]
    lines += ["", f"Expires in {format_remaining(request.expires_at, now)}"]
    return _fit(lines)


def render_resolved(request: ApprovalRequest, *, decision: str, by: str | None) -> str:
    """What the card becomes once it has been answered.

    The message is edited in place rather than replaced, so scrolling back
    through a chat shows what was decided instead of a row of live-looking
    buttons on questions that were settled hours ago.
    """
    mark = "✅ ALLOWED" if decision == "allow" else "⛔ DENIED"
    who = f" by {html.escape(by)}" if by else ""
    return _fit(
        [
            f"<b>{mark}</b>{who}",
            "",
            f"Project: <code>{html.escape(request.project)}</code>",
            f"Tool: <code>{html.escape(request.tool)}</code>",
            "",
            f"<pre>{html.escape(request.command_summary)}</pre>",
        ]
    )


def keyboard(request: ApprovalRequest, *, include_full: bool) -> dict:
    """The buttons under a card.

    Allow and Deny sit on their own row, away from anything harmless, so a
    mistimed tap on 'show the rest of this' cannot land on 'allow'.
    """
    rows = [
        [
            {"text": "Allow once", "callback_data": callback_data(request, ALLOW)},
            {"text": "Deny", "callback_data": callback_data(request, DENY)},
        ]
    ]
    if include_full:
        rows.append(
            [{"text": "Show full command", "callback_data": callback_data(request, SHOW_FULL)}]
        )
    return {"inline_keyboard": rows}


def _short_session(session_id: str) -> str:
    return session_id if len(session_id) <= 13 else f"{session_id[:4]}…{session_id[-4:]}"


def _fit(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= MESSAGE_LIMIT:
        return text
    # The command is already summarised by `Redactor.prepare`, so reaching this
    # means something else grew. Cut rather than let the API reject the card.
    return text[: MESSAGE_LIMIT - 1] + "…"
