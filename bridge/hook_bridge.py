#!/usr/bin/env python3
"""Relay an agent permission request to the Halyard control plane.

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

Configuration is looked up rather than demanded — see `_settings.py`. A hook
inherits the shell Claude Code was launched from, and requiring `HALYARD_URL` to
be exported there would mean every forgotten export turns into a denied command:

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

from _settings import codex_thread_name, control_plane_url, note, runtime_of, session_name
from _settings import timeout as lookup_timeout

DEFAULT_TIMEOUT_SECONDS = 330.0

#: Exit code meaning "deliberately no opinion", understood by `hook.sh`.
#: Anything else with empty output is a crash, and a crash denies.
DEFER_EXIT_CODE = 64


def emit(event: str, decision: str, reason: str) -> None:
    """Print a hook decision and nothing else.

    PreToolUse and PermissionRequest look similar on input but deliberately use
    different decision schemas. The former gates every tool call. The latter is
    Codex's separate answer to a native sandbox-escalation prompt.
    """
    if event == "PermissionRequest":
        specific = {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision, "message": reason},
        }
    else:
        specific = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    json.dump(
        {"hookSpecificOutput": specific},
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def deny(reason: str, event: str = "PreToolUse") -> None:
    emit(event, "deny", f"Denied by Halyard: {reason}")


def build_body(payload: dict) -> dict:
    """Turn a hook payload into a control plane request.

    Kept as close to a copy as possible. Anything clever here is logic that
    lives outside the tested part of the system.
    """
    # Which runtime raised this, and what its session is called. Both are
    # needed before anything can be routed: a Claude driver and a Codex driver
    # are both `driver`, and a card that cannot say which one it came from
    # belongs to neither and lands in the default chat.
    transcript = payload.get("transcript_path")
    runtime = runtime_of(transcript)
    name = (
        codex_thread_name(payload.get("session_id"))
        if runtime == "codex"
        else session_name(transcript)
    )

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        # Phase 1 only relays Bash. Anything else still gets described rather
        # than silently summarised to nothing, and core redacts it either way.
        command = json.dumps(tool_input, ensure_ascii=False)
    return {
        "session_id": payload.get("session_id") or "unknown",
        "agent_id": runtime,
        "tool": payload.get("tool_name") or "unknown",
        "command": command,
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
        # Which project this came from, so a card can say so. Passed on rather
        # than turned into a name here — the bridge is a courier, and deciding
        # what to call a project is core's job.
        "project_dir": os.environ.get("CLAUDE_PROJECT_DIR"),
        # Which seat this session is sitting in. Two Claude Code sessions on one
        # codebase look identical to a hook except for session_id, and that
        # changes every restart — so the role has to come from whoever launched
        # them: HALYARD_ROLE=navigator claude
        "role": os.environ.get("HALYARD_ROLE") or None,
        # The name the session carries in the desktop app, where there is
        # no shell to put HALYARD_ROLE in. Stable across restarts, unlike
        # session_id.
        "session_name": name,
        "reason": tool_input.get("justification")
        if isinstance(tool_input.get("justification"), str)
        else None,
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
    event = "PreToolUse"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload was not an object")
        if payload.get("hook_event_name") == "PermissionRequest":
            event = "PermissionRequest"
    except Exception as exc:
        deny(f"the hook payload could not be read ({exc}).", event)
        return 0

    url = control_plane_url()
    timeout = lookup_timeout("HALYARD_BRIDGE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    body = build_body(payload)
    # Written before the call, not after, so a bridge that hangs or is killed
    # still leaves evidence that it ran. "Did the hook fire at all" had no
    # answer anywhere until this line existed.
    note(
        f"{body['agent_id']} {body['tool']} session={body['session_name'] or body['session_id']} "
        f"-> {url}"
    )

    try:
        answer = ask(url, body, timeout)
    except urllib.error.URLError as exc:
        note(f"unreachable: {exc.reason}")
        deny(
            f"the control plane at {url} could not be reached ({exc.reason}). Failing closed.",
            event,
        )
        return 0
    except TimeoutError:
        note(f"no answer within {timeout:g}s")
        deny(
            f"the control plane at {url} did not answer within {timeout:g}s. Failing closed.",
            event,
        )
        return 0
    except Exception as exc:
        deny(f"the control plane at {url} failed ({exc}). Failing closed.", event)
        return 0

    decision = answer.get("decision")
    reason = answer.get("reason") or "no reason given"

    # Only an exact allow allows. A missing field, a typo, a null, a decision
    # this bridge has never heard of — all of them mean deny.
    if decision == "allow":
        emit(event, "allow", reason)
    elif decision == "defer":
        # Halyard is paused: no opinion, so Claude Code decides on its own the
        # way it would if this hook were not installed. Held to the same
        # narrowness as allow — only the exact word does this.
        #
        # Signalled by exit code rather than by silence. `hook.sh` treats empty
        # output as "this script died", which is the right default and is what
        # keeps a crash from being read as consent — so a deliberate silence
        # has to be distinguishable from an accidental one, or pausing gets
        # turned into denying everything. It did, once.
        return DEFER_EXIT_CODE
    else:
        emit(event, "deny", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
