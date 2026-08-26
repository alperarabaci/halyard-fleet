#!/usr/bin/env python3
"""Hand a session its orientation back after a compaction.

Wired to `SessionStart`, which fires both when a session opens and again once a
compaction has finished — the two are told apart by `source`, which is
`"startup"` the first time and `"compact"` the second. Only the second is acted
on: an opening session has its context, and a compacted one has just lost half
of it.

Standard library only, like the other three bridges. Prints an
`additionalContext` for Claude Code to fold into the session, and that channel
is measured to work — a token injected here came back verbatim from the model
afterwards, where the same token injected at `PreCompact` did not.

**Fails open, and silently.** Every path that cannot produce text prints
nothing and exits 0, which leaves the session exactly as the compaction left
it. A session that will not start is a far worse outcome than one that starts
without a page of orientation, and nothing here is holding a decision.
"""

from __future__ import annotations

import json
import sys
import urllib.request

from _settings import control_plane_url, note, runtime_of, session_name
from _settings import timeout as lookup_timeout

#: The `before` call is held open while the record is written — the control
#: plane gives up on that at 120 seconds — so this sits above it and below the
#: 180 the hook itself allows. The `after` call only reads and returns at once.
DEFAULT_TIMEOUT_SECONDS = 150.0

#: The one `source` worth acting on. Measured against a live session.
AFTER_COMPACTION = "compact"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event == "SessionStart" and payload.get("source") != AFTER_COMPACTION:
            # An ordinary start. Nothing was lost, so nothing is restored.
            return 0
        if event not in ("SessionStart", "PreCompact"):
            return 0

        transcript = payload.get("transcript_path")
        body = {
            "session_id": payload.get("session_id") or "unknown",
            "agent_id": runtime_of(transcript),
            "session_name": session_name(transcript),
            # `before` starts the record and returns at once; `after` collects
            # it. Two moments, one script, because they are one feature.
            "when": "before" if event == "PreCompact" else "after",
        }
        note(f"{body['when']} compaction: {body['session_name'] or body['session_id']}")

        url = control_plane_url().rstrip("/") + "/v1/compaction"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = lookup_timeout("HALYARD_RELAY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return 0
            answer = json.loads(response.read().decode("utf-8"))

        if body["when"] == "before":
            # Nothing to print: the record is collected at `SessionStart`, once
            # the summary has been made. This call existed to hold the
            # compaction while it was written.
            return 0

        text = answer.get("context")
        if not isinstance(text, str) or not text.strip():
            # This seat has nothing configured, which is the common case.
            return 0

        json.dump(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    except Exception:
        # Swallowed on purpose; see the module docstring. There is no failure
        # here worth interrupting a session's restart over.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
