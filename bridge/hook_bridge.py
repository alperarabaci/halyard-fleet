#!/usr/bin/env python3
"""Relay a Claude Code permission request to the Halyard control plane.

Standard library only, no package imports, no configuration file. This script
runs inside somebody else's process tree on every tool call, so it has to work
when nothing else does. Read stdin, make one HTTP call, print a decision.

**It denies on everything.** Unreachable control plane, timeout, a 5xx, a
response it cannot parse, a response that is not exactly `allow` — all denials.
There is no path through this file that lets a command run because something
went wrong.

That is not paranoia, it is what `docs/hook-payload-notes.md` recorded by
experiment: Claude Code treats malformed output, empty output, and any non-zero
exit other than 2 as *no opinion*, and runs the command. A bridge cannot express
refusal by failing — it has to print one. So this script prints a decision on
every path and exits 0, and `bridge/hook.sh` covers the case where it never got
far enough to print anything at all.

Configuration comes from the environment:

    HALYARD_URL                       default http://127.0.0.1:8787
    HALYARD_BRIDGE_TIMEOUT_SECONDS    default 330

The timeout must sit above the control plane's approval deadline and below the
hook timeout in settings.json. See `Settings` in `halyard/config.py`, which
refuses to start if that ordering is broken.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT_SECONDS = 330.0


def emit(decision: str, reason: str) -> None:
    """Print a hook decision and nothing else.

    The wrapper form rather than the legacy `{"decision": "block"}` one. Both
    were observed to work; only this one is documented, which makes the other
    the one that disappears in a future release.
    """
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def deny(reason: str) -> None:
    emit("deny", f"Denied by Halyard: {reason}")


def build_body(payload: dict) -> dict:
    """Turn a hook payload into a control plane request.

    Kept as close to a copy as possible. Anything clever here is logic that
    lives outside the tested part of the system.
    """
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        # Phase 1 only relays Bash. Anything else still gets described rather
        # than silently summarised to nothing, and core redacts it either way.
        command = json.dumps(tool_input, ensure_ascii=False)
    return {
        "session_id": payload.get("session_id") or "unknown",
        "agent_id": "claude-code",
        "tool": payload.get("tool_name") or "unknown",
        "command": command,
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
    }


def ask(url: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/approvals",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"control plane answered {response.status}")
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload was not an object")
    except Exception as exc:
        deny(f"the hook payload could not be read ({exc}).")
        return 0

    url = os.environ.get("HALYARD_URL", DEFAULT_URL)
    try:
        timeout = float(os.environ.get("HALYARD_BRIDGE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS

    try:
        answer = ask(url, build_body(payload), timeout)
    except urllib.error.URLError as exc:
        deny(f"the control plane at {url} could not be reached ({exc.reason}). Failing closed.")
        return 0
    except TimeoutError:
        deny(f"the control plane at {url} did not answer within {timeout:g}s. Failing closed.")
        return 0
    except Exception as exc:
        deny(f"the control plane at {url} failed ({exc}). Failing closed.")
        return 0

    reason = answer.get("reason") or "no reason given"
    # Only an exact allow allows. A missing field, a typo, a null, a decision
    # this bridge has never heard of — all of them mean deny.
    if answer.get("decision") == "allow":
        emit("allow", reason)
    else:
        emit("deny", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
