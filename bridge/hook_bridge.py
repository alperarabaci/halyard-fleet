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

from _settings import (
    antigravity_title,
    codex_thread_name,
    control_plane_url,
    last_assistant_text,
    note,
    runtime_of,
    session_name,
)
from _settings import timeout as lookup_timeout

DEFAULT_TIMEOUT_SECONDS = 330.0

#: Exit code meaning "deliberately no opinion", understood by `hook.sh`.
#: Anything else with empty output is a crash, and a crash denies.
DEFER_EXIT_CODE = 64


def _resources(command: str) -> list[str]:
    """The resource strings that name exactly this call.

    A literal, not a pattern and not a wildcard — read from Antigravity's own
    record, the `step_payload` of a step in `conversations/<id>.db`, which
    holds the hook's answer and the application's requirement side by side.

    `command(...)` works: measured across six live runs on one conversation,
    the single call whose record wanted a `command` resource was granted with
    no second prompt.

    **`unsandboxed(...)` does not, and is kept anyway.** Every call whose
    record required `unsandboxed(*)` asked again, including the run that
    returned exactly that string — and Antigravity confirms this is by design:
    an `unsandboxed` or `escalate_admin` override from a hook is ignored,
    because honouring one would let whoever controls the hook run unsandboxed
    code on the machine. That is the right call, and Halyard does not try to
    get around it.

    It stays because the shape is correct and only the policy refuses it. If
    that policy ever gains a way to say yes — a signed hook, a per-project
    opt-in — this is the line that starts working, and deleting it would mean
    rediscovering the spelling that took six runs to establish. A
    `run_command` carrying `BypassSandbox: true` asks at the desk until then.
    """
    return [f"command({command})", f"unsandboxed({command})"]


def emit(
    event: str,
    decision: str,
    reason: str,
    runtime: str = "claude-code",
    grant: str | None = None,
) -> None:
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
    # One shape per runtime, never a merge. Antigravity reads a flat
    # `decision`; Claude Code and Codex read `hookSpecificOutput`. Measured, in
    # three runs against a live conversation with a hook proven to have fired:
    #
    #   hookSpecificOutput only          command ran
    #   both keys in one object          command ran
    #   flat `decision` only             command blocked, no prompt at all
    #
    # So the extra key is not ignored — it stops the answer being understood at
    # all, and an unreadable answer is an approval. A payload that tries to
    # serve every runtime serves none.
    answer: dict = (
        {"decision": decision, "reason": reason}
        if runtime == "antigravity"
        else {"hookSpecificOutput": specific}
    )
    # Antigravity asks twice otherwise. `allow` from a hook means this hook
    # does not object; it is not the same as the tool being permitted, and its
    # own permission layer still stops to ask — so somebody who has already
    # answered on their phone is asked again at the desk, which is the thing
    # Halyard exists to remove.
    #
    # `permissionOverrides` is what grants it — see `_resources`. Sent only
    # alongside an allow, so a refusal can never hand out a permission, and
    # only when there was a command to name.
    if grant and runtime == "antigravity" and decision == "allow":
        answer["permissionOverrides"] = _resources(grant)
    # Recorded before it goes out. Every question about this path so far has
    # started with "what did we actually send?", and answering it from the
    # source rather than from the wire has been wrong twice.
    if runtime == "antigravity":
        note(f"PreToolUse -> {json.dumps(answer)}")

    # `ensure_ascii=False`: the reason carries text a person wrote, and it
    # reaches the agent. Escaping it to `\u00e7` sequences is valid JSON and
    # unreadable to anybody reading the transcript afterwards.
    json.dump(answer, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def deny(reason: str, event: str = "PreToolUse", runtime: str = "claude-code") -> None:
    emit(event, "deny", f"Denied by Halyard: {reason}", runtime)


#: How much of the agent's own prose to carry onto a card, when it is all there
#: is. Long enough for the paragraph that explains a command, short enough that
#: the command stays the thing being read — a card whose context scrolls is a
#: card nobody finishes.
CONTEXT_LIMIT = 600


def _context(tool_input: dict, transcript: str | None) -> str | None:
    """Why this call is being made, in the agent's own words.

    The tool's own reason first and, when there is one, alone. This used to
    append the last thing the agent said in the conversation as well, and that
    was wrong in a way that only showed up in use: the last assistant message is
    the agent's report *to a person* — what it found, what it ran, what it
    thinks — and not a justification for the command about to run.

    Measured on forty real cards: thirty-eight carried both, and the chat text
    averaged 203 characters against the reason's 45. Four fifths of the card was
    about something else, on ninety-five per cent of cards, and the one line
    that answers "should this run" was buried under it.

    The fallback stays because it earns its place where nothing else answers:
    a tool with no `description` leaves a card with nothing on it but a command.
    """
    summary = tool_input.get("description") or tool_input.get("justification")
    summary = summary.strip() if isinstance(summary, str) else ""
    if summary:
        return summary

    said = (last_assistant_text(transcript) or "").strip()
    if not said:
        return None
    return said[:CONTEXT_LIMIT] + ("…" if len(said) > CONTEXT_LIMIT else "")


def build_body(payload: dict) -> dict:
    """Turn a hook payload into a control plane request.

    Kept as close to a copy as possible. Anything clever here is logic that
    lives outside the tested part of the system.
    """
    # Which runtime raised this, and what its session is called. Both are
    # needed before anything can be routed: a Claude driver and a Codex driver
    # are both `driver`, and a card that cannot say which one it came from
    # belongs to neither and lands in the default chat.
    # Antigravity spells every field differently: `transcriptPath` rather than
    # `transcript_path`, `conversationId` rather than `session_id`, and the
    # tool under `toolCall` rather than `tool_name`/`tool_input`. Measured from
    # its own documentation and confirmed against a live hook.
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    runtime = runtime_of(transcript)

    if runtime == "antigravity":
        conversation = payload.get("conversationId") or "unknown"
        call = payload.get("toolCall") or {}
        arguments = call.get("args") or {}
        tool = call.get("name") or "unknown"
        command = arguments.get("CommandLine")
        # `workspacePaths` arrives with every call, so where the work is
        # happening never has to be looked up — which matters, because nothing
        # in an Antigravity transcript records it.
        workspaces = payload.get("workspacePaths") or []
        return {
            "session_id": conversation,
            "agent_id": runtime,
            "tool": tool,
            "command": command
            if isinstance(command, str)
            else json.dumps(arguments, ensure_ascii=False),
            "tool_use_id": (
                str(payload["stepIdx"]) if payload.get("stepIdx") is not None else None
            ),
            "cwd": arguments.get("Cwd") or (workspaces[0] if workspaces else None),
            "project_dir": workspaces[0] if workspaces else None,
            "role": os.environ.get("HALYARD_ROLE") or None,
            "session_name": antigravity_title(conversation),
        }

    name = (
        codex_thread_name(payload.get("session_id"))
        if runtime == "codex"
        else session_name(transcript)
    )

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        file_path = None
    if not isinstance(command, str):
        if file_path:
            # A file tool. The card wants the destination, not the file — a
            # `Write` carries its whole content in `tool_input`, and putting
            # that on a phone would bury the one fact worth reading.
            command = f"{payload.get('tool_name') or 'write'} {file_path}"
        else:
            # Anything else still gets described rather than silently summarised
            # to nothing, and core redacts it either way.
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
        # The destination of a file tool, passed separately from the summary so
        # the control plane can match it against `writes:` without parsing prose.
        "file_path": file_path,
        # The context a person has on screen and a card did not.
        #
        # Two sources, best first. `description` is the one-line summary the
        # tool call carries — "Prove the wizard's output is what the control
        # plane reads" — and it is in the payload, so it costs nothing and is
        # always there for a Bash call. Only `justification` was being read,
        # which Bash calls do not have, so every card said nothing.
        #
        # The prose above the command is richer and lives in the transcript,
        # because the payload does not carry it. Read second, appended, and
        # bounded: a bare command on a phone is a thing to approve with the
        # intent guessed from the shell.
        "reason": _context(tool_input, transcript),
    }


def ask(url: str, body: dict, timeout: float, endpoint: str = "/v1/approvals") -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"control plane answered {response.status}")
        return json.loads(response.read().decode("utf-8"))


def emit_answers(updated_input: dict) -> None:
    """Answer an AskUserQuestion by filling the choice straight into its input.

    Not a permission decision — an `allow` that also *rewrites the tool's
    arguments*. `updatedInput` carries an `answers` object keyed by each
    question's text, and Claude Code proceeds as if the person had chosen at the
    desk: the terminal picker never appears. Measured against a live session
    before this was built on it; see `docs/session-io-notes.md`.

    Claude Code only. The field is read from `hookSpecificOutput`, which is the
    shape Antigravity's parser chokes on — but Antigravity has no such tool, so
    this path is never reached for it.
    """
    answer = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }
    json.dump(answer, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _single_question(tool_input: dict) -> dict | None:
    """The one single-select question we can answer from a phone, or None.

    The MVP handles exactly one question, not multi-select. More than one, or a
    `multiSelect` option, is left to the terminal rather than folded into
    something a row of buttons cannot honestly represent — and `note`d, so a
    silent cap does not read as coverage.
    """
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        if questions:
            note(f"AskUserQuestion: {len(questions)} questions, leaving it to the terminal")
        return None
    question = questions[0]
    if not isinstance(question, dict) or question.get("multiSelect"):
        note("AskUserQuestion: multiSelect, leaving it to the terminal")
        return None
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return None
    return question


def question_body(payload: dict, question: dict) -> dict:
    """What `/v1/questions` is posted. Claude Code fields only, like the tool."""
    transcript = payload.get("transcript_path")
    options = [
        {"label": o.get("label"), "description": o.get("description")}
        for o in question.get("options", [])
        if isinstance(o, dict) and o.get("label")
    ]
    return {
        "session_id": payload.get("session_id") or "unknown",
        "agent_id": "claude-code",
        "question": question.get("question") or "",
        "header": question.get("header"),
        "options": options,
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
        "project_dir": os.environ.get("CLAUDE_PROJECT_DIR"),
        "role": os.environ.get("HALYARD_ROLE") or None,
        "session_name": session_name(transcript),
    }


def answer_question(payload: dict, url: str, timeout: float) -> int:
    """Ask the phone to choose, and fill the answer in — or defer to the terminal.

    Fails open, the mirror of the approval path. Every way this can go wrong —
    an unanswerable shape, an unreachable control plane, a timeout, no answer
    before the deadline — ends in `DEFER_EXIT_CODE`, which says nothing and lets
    the terminal picker take the choice. A question is not dangerous to leave to
    the desk the way a command is dangerous to leave unapproved.
    """
    tool_input = payload.get("tool_input") or {}
    question = _single_question(tool_input)
    if question is None:
        return DEFER_EXIT_CODE

    body = question_body(payload, question)
    if not body["options"]:
        return DEFER_EXIT_CODE

    try:
        answer = ask(url, body, timeout, endpoint="/v1/questions").get("answer")
    except Exception as exc:
        note(f"question fell back to the terminal: {exc}")
        return DEFER_EXIT_CODE

    if not isinstance(answer, str) or not answer:
        # Nobody chose, the gate was paused, or delivery failed. The terminal
        # picker is still standing and gets it.
        return DEFER_EXIT_CODE

    # Keyed by the question text, exactly as measured. The rest of the input is
    # carried through untouched so the tool sees what it expected plus the answer.
    updated = {**tool_input, "answers": {question.get("question"): answer}}
    emit_answers(updated)
    return 0


def main() -> int:
    event = "PreToolUse"
    # Assumed until the payload says otherwise, so that a payload which cannot
    # even be parsed still produces a denial in *some* dialect rather than none.
    runtime = "claude-code"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload was not an object")
        if payload.get("hook_event_name") == "PermissionRequest":
            event = "PermissionRequest"
    except Exception as exc:
        deny(f"the hook payload could not be read ({exc}).", event, runtime)
        return 0

    url = control_plane_url()
    timeout = lookup_timeout("HALYARD_BRIDGE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)

    # A different kind of question: the agent asking a person to choose, not to
    # approve. Only Claude Code has the tool, and only Claude Code reads the
    # `updatedInput` this answers it with. Everything about this path fails open
    # — a question left unanswered goes back to the terminal picker, which is
    # why it is kept out of the deny-on-everything flow below.
    if event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        return answer_question(payload, url, timeout)

    body = build_body(payload)
    runtime = body["agent_id"]
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
            runtime,
        )
        return 0
    except TimeoutError:
        note(f"no answer within {timeout:g}s")
        deny(
            f"the control plane at {url} did not answer within {timeout:g}s. Failing closed.",
            event,
            runtime,
        )
        return 0
    except Exception as exc:
        deny(f"the control plane at {url} failed ({exc}). Failing closed.", event, runtime)
        return 0

    decision = answer.get("decision")
    reason = answer.get("reason") or "no reason given"

    # Only an exact allow allows. A missing field, a typo, a null, a decision
    # this bridge has never heard of — all of them mean deny.
    if decision == "allow":
        emit(event, "allow", reason, runtime, grant=body.get("command"))
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
        emit(event, "deny", reason, runtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
