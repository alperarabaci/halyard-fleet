"""Asking Halyard's own gate about a tool a ZCode turn wants to run.

Every other runtime asks through `hook_bridge.py`, which posts the tool to
`/v1/approvals` and waits for the answer. ZCode's engine asks its host instead,
and the host is Halyard — so the question goes to the same place by the same
road: one card, the project's own `writes:` and `tools:` rules, the same audit
line. A second way of deciding would be a second way of being wrong.

**A paused gate refuses here.** Elsewhere `defer` means Halyard steps aside and
the runtime asks the person at the desk. Nobody is at this desk — the engine is
waiting on Halyard and will wait for ever — so a paused gate is a refusal with
a reason that says so, rather than a turn that hangs until somebody notices.
"""

from __future__ import annotations

import json
import logging

import httpx

from halyard.agents.zcode.protocol import Answer, Asking, Permission

logger = logging.getLogger(__name__)

#: The risks the control plane knows, so ZCode's own word is passed on only
#: when it is one of them and left out when it is not.
RISKS = ("low", "medium", "high")


def through(url: str, *, timeout: float, agent: str = "zcode") -> Asking:
    """Ask that control plane, at that address, about each tool."""

    async def asking(request: Permission) -> Answer:
        body = {
            "session_id": request.session_id,
            "agent_id": agent,
            "tool": request.tool or "tool",
            "command": _said(request),
            "tool_use_id": request.tool_call_id or None,
            "cwd": request.about.get("cwd"),
            "project_dir": request.about.get("cwd"),
            "session_name": request.about.get("session_name"),
            "file_path": _path(request),
            "declared_risk": request.risk if request.risk in RISKS else None,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                answered = await client.post(f"{url}/v1/approvals", json=body)
                answered.raise_for_status()
                said = answered.json()
        except (httpx.HTTPError, ValueError) as unreachable:
            # The same rule the approval bridge has: unanswerable is refused.
            logger.warning("The gate could not be asked about %s: %s", request.tool, unreachable)
            return Answer(decision="deny", reason="Halyard could not be reached")
        decision = str(said.get("decision") or "deny")
        reason = str(said.get("reason") or "")
        if decision == "allow":
            return Answer(decision="allow", reason=reason)
        if decision == "defer":
            return Answer(
                decision="deny",
                reason="Halyard's gate is paused, and nothing else can answer for this session",
            )
        return Answer(decision="deny", reason=reason or "denied")

    return asking


def _said(request: Permission) -> str:
    """What the tool was asked to do, in one line for the card.

    The shell's own line where there is one, the file where the tool names one,
    and the whole input otherwise — a card that says only `Write` tells nobody
    what they are approving.
    """
    for field in ("command", "file_path", "path", "pattern", "url"):
        if isinstance(value := request.input.get(field), str) and value.strip():
            return value.strip()
    try:
        return json.dumps(request.input)[:400] or request.tool
    except (TypeError, ValueError):
        return request.tool


def _path(request: Permission) -> str | None:
    """The file a write would land on, for the `writes:` rules to read."""
    found = request.input.get("file_path") or request.input.get("path")
    return found if isinstance(found, str) and found.strip() else None
