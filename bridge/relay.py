#!/usr/bin/env python3
"""Relay an agent's reply to the Halyard control plane.

Wired to the `Stop` hook, which fires once per turn and carries the assistant's
final message in `last_assistant_message` (measured — see
`docs/session-io-notes.md`). Standard library only, like the approval bridge.

**This one fails open, and that is deliberate.** `hook_bridge.py` denies on
every error because a permission request that goes unanswered would otherwise
let a command run unsupervised. Nothing here is holding a decision: a relay that
cannot reach the control plane has lost a chat message, not lost control of the
machine. Blocking the agent's turn over an undelivered notification would be a
worse outcome than the missing notification.

So every path here ends in exit 0 with empty stdout — "no opinion" — and the
session carries on regardless. The two bridges have opposite rules on purpose,
which is why they are two files rather than one with a branch in it.

Configuration is looked up rather than demanded — see `_settings.py`:

    HALYARD_URL                      default http://127.0.0.1:8787
    HALYARD_RELAY_TIMEOUT_SECONDS    default 5
"""

from __future__ import annotations

import json
import sys
import urllib.request

from _settings import control_plane_url
from _settings import timeout as lookup_timeout

#: Short on purpose. The agent's turn is waiting on this, and a slow relay is
#: not worth stalling the session for.
DEFAULT_TIMEOUT_SECONDS = 5.0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0

        text = payload.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            # Nothing was said — a turn that only ran tools, for instance.
            return 0

        body = {
            "session_id": payload.get("session_id") or "unknown",
            "agent_id": "claude-code",
            "text": text,
            "cwd": payload.get("cwd"),
        }

        url = control_plane_url()
        timeout = lookup_timeout("HALYARD_RELAY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)

        request = urllib.request.Request(
            url.rstrip("/") + "/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except Exception:
        # Swallowed on purpose. See the module docstring: there is no failure
        # here worth interrupting the agent over.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
