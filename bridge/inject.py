#!/usr/bin/env python3
"""Hand Antigravity whatever a person said while it was not listening.

Wired to `PreInvocation`, which fires before the model is called and is the one
hook that can answer with `injectSteps`. A `{"userMessage": ...}` step arrives
as a turn the person typed — not as the `SYSTEM_MESSAGE` that `agentapi
send-message` is limited to, and which Antigravity draws under a "Message from
System" header with its own sentence saying the user did not send it.

Measured with a one-shot probe hook wired beside the gate: the model obeyed an
instruction that existed only in the injected step, and the step is written to
neither `transcript.jsonl` nor `transcript_full.jsonl`. It reaches the model
and the screen, and leaves nothing on disk — so the control plane's queue is
emptied by this request rather than by anything the runtime reports back.

**This fails open, like the relay and unlike the gate.** Nothing here is
holding a decision. An unreachable control plane means a message is not
delivered, which is a lost notification rather than a command running
unsupervised — and stalling every model call in the session behind an HTTP
timeout would be a far worse outcome than the missing message. So every path
ends in exit 0, and silence means "nothing to inject".

    HALYARD_URL                      default http://127.0.0.1:8787
    HALYARD_INJECT_TIMEOUT_SECONDS   default 5
"""

from __future__ import annotations

import json
import sys
import urllib.request

from _settings import control_plane_url, note
from _settings import timeout as lookup_timeout

#: Short, because the model call is waiting on it. `PreInvocation` runs before
#: every invocation, so this cost is paid on each one.
DEFAULT_TIMEOUT_SECONDS = 5.0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0

        conversation = payload.get("conversationId")
        if not conversation:
            # Not an Antigravity PreInvocation payload. Nothing to say about it.
            return 0

        body = {"session_id": conversation, "agent_id": "antigravity"}
        request = urllib.request.Request(
            control_plane_url().rstrip("/") + "/v1/inject",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = lookup_timeout("HALYARD_INJECT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            reply = json.loads(answer.read() or b"{}")

        steps = reply.get("injectSteps")
        if not isinstance(steps, list) or not steps:
            # Nothing waiting. Printing an empty `injectSteps` would be the
            # same thing said at more length, and every byte here is read by
            # something that has to parse it before the model can run.
            return 0

        note(f"antigravity PreInvocation: injecting {len(steps)} step(s) into {conversation}")
        print(json.dumps({"injectSteps": steps}))
    except Exception:
        # Swallowed on purpose. See the module docstring.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
