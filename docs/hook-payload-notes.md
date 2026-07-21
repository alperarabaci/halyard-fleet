# Claude Code hook payload notes

Everything downstream of this file — the bridge, the approval contract, the fail-closed
guarantees — is built on what is recorded here. This is the one place where **observation beats
documentation**. Where an observed value contradicted the documented one, the observed value won
and the contradiction is written down.

- **Claude Code version observed:** 2.x (Claude Desktop harness, `permission_mode: acceptEdits`)
- **Platform:** macOS (darwin 25.5.0)
- **Observed on:** 2026-07-20
- **Payloads captured:** 13 (Bash, Write, Edit)
- **Status:** ✅ complete — all five questions answered by experiment, not by reading

---

## TL;DR — the three findings that shape the design

1. **Claude Code fails open on every ambiguous outcome.** Malformed stdout, empty stdout, and a
   non-zero exit code other than 2 all let the tool run. There is no "the hook broke, so deny"
   default. The bridge must *print an explicit deny* on every error path; it can never express a
   denial by failing.
2. **A hook that times out fails open.** When the hook exceeded its configured timeout the command
   executed. This is the sharpest edge in the system: if the user does not answer in time and the
   bridge is still blocking, Claude Code eventually gives up *and runs the command anyway*. The
   approval timeout must therefore expire strictly before the hook timeout, so the bridge is always
   the one that answers.
3. **`tool_use_id` exists and is not in the documentation.** It is a stable, per-tool-call
   identifier supplied by Claude Code. Use it for correlation instead of inventing one.

---

## How the observation was run

A hook was registered in `.claude/settings.local.json` (local and gitignored — this is throwaway
scaffolding and should not ship as an active hook in a public repo):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/bridge/observe.sh", "timeout": 10 }
        ]
      }
    ]
  }
}
```

`bridge/observe.sh` appends stdin to `/tmp/halyard-hook.log` and normally exits 0 writing nothing.
It also carries marker-triggered probes that fire only for Bash calls, which is how the decision
formats and failure modes below were tested.

Two practical notes:

- **Claude Code snapshots hook configuration at session start.** Editing `settings.local.json`
  mid-session has no effect; the session must be restarted. This was confirmed by observing that
  Bash calls made immediately after writing the file produced no log entries.
- **The script body is *not* snapshotted.** It is read at execution time, so `observe.sh` could be
  rewritten repeatedly and each probe took effect immediately, with no restart.

---

## 1. What fields does the payload actually contain?

A real Bash payload, with only the command and description values substituted:

```json
{
  "session_id": "a84ff5b4-289e-46af-be80-b88bb10a4349",
  "transcript_path": "/Users/…/-Users-jammer-Documents-dev-ai-halyard-fleet/a84ff5b4-….jsonl",
  "cwd": "/Users/jammer/Documents/dev/ai/halyard-fleet",
  "prompt_id": "f79eca6d-c72b-4aac-adee-182b857df842",
  "permission_mode": "acceptEdits",
  "effort": { "level": "xhigh" },
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "git status --short", "description": "Show working tree status" },
  "tool_use_id": "toolu_01UV5C7zg2NkogWrtQTSDowu"
}
```

| Field | Type | Notes |
|---|---|---|
| `session_id` | string (uuid) | Stable for the session. Matches the transcript filename stem. |
| `transcript_path` | string (path) | Full JSONL transcript. Not read in Phase 1, but this is the hook into richer context later. |
| `cwd` | string (path) | Working directory of the session. |
| `prompt_id` | string (uuid) | Identifies the user turn. Several tool calls in one turn share it — useful for grouping later, unused in Phase 1. |
| `permission_mode` | string | Observed `acceptEdits`. Documented values include `default`, `plan`, `acceptEdits`, `bypassPermissions`. |
| `effort` | object | `{"level": "xhigh"}`. Not useful to us. |
| `hook_event_name` | string | Always `PreToolUse` here. |
| `tool_name` | string | `Bash`, `Write`, `Edit`, … |
| `tool_input` | object | Tool-specific; see §5. |
| `tool_use_id` | string | **Undocumented.** `toolu_…`. Unique per tool call. |

**Deviations from the documented baseline:**

- `tool_use_id` is present but absent from the docs. It is exactly the correlation key the approval
  contract needs, so `ApprovalRequest` should carry it rather than relying only on a generated
  `request_id`.
- `agent_id` / `agent_type` were never present. Consistent with the docs, which say they appear only
  in subagents. Not yet verified for subagent calls.
- No field carries the agent's *reason* for the call. `tool_input.description` is the closest thing
  — a short model-written summary ("Show working tree status") — but it describes *what* the command
  does, not *why* it is needed. **The `reason` field on `ApprovalRequest` has no source in Phase 1
  and must be nullable.** Getting a real rationale requires Phase 2's message channel.

---

## 2. What output format is accepted for a permission decision?

**Both forms work.** Tested by making the hook emit each and observing whether the command ran.

Documented wrapper form — denies, reason surfaced verbatim:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Halyard probe: denied via the hookSpecificOutput wrapper form."
  }
}
```

Legacy bare form — **also still honored**, despite not appearing in the current docs:

```json
{ "decision": "block", "reason": "Halyard probe: denied via the legacy decision/block form." }
```

Both produced an identical result: the command did not run, and the reason string came back as the
tool's error output.

**Decision for the bridge: emit the wrapper form.** The legacy form works today but is
undocumented, which makes it exactly the thing that disappears in a future release.

`permissionDecision` values per the docs are `allow` / `deny` / `ask` / `defer`. Only `deny` was
exercised against a real session; `allow` matters less than it seems, because the safe posture is to
return `deny` on doubt and let the normal permission flow handle everything else.

---

## 3. What is the hook timeout, and what happens when it is exceeded?

- Documented default: **600 seconds**.
- Configured per hook via a `timeout` field in seconds, inside the individual hook object.
- Observed with `timeout: 10` and a hook that slept 25 seconds:

> **The command ran.** A hook that exceeds its timeout is discarded and the tool proceeds.

This is fail-open, and it is the constraint the whole timing design hangs off. The bridge must
always answer first. Required ordering:

```
HALYARD_APPROVAL_TIMEOUT_SECONDS  <  bridge HTTP timeout  <  hook timeout
        300s                              330s                  600s
```

Consequences:

- The hook `timeout` must be set **explicitly** in settings, never left to the default, so the whole
  relationship is visible in one place.
- The bridge needs its own HTTP timeout, and must print a deny when it fires. Waiting indefinitely
  on the control plane is the one thing it must never do — an indefinite wait becomes an *allow*.
- If the approval timeout is ever raised, the hook timeout has to move with it. This ordering
  belongs in a startup assertion, not a comment.

---

## 4. What does Claude Code do on deny?

The tool does not run. The `permissionDecisionReason` string is delivered to the model **verbatim**
as the tool's error result:

```
<error>Halyard probe: denied via the hookSpecificOutput wrapper form.</error>
```

The session continues normally — the model simply sees a failed tool call and carries on, free to
explain, adapt, or try something narrower.

This makes the reason string the only channel back into the session in Phase 1, and it is a good
one. Denials should say *why* in a form the model can act on — `"Denied by <user> from Telegram"`,
`"No answer within 5 minutes, denied by timeout"`, `"Halyard control plane unreachable, failing
closed"` — so the agent can respond sensibly instead of blindly retrying.

---

## 5. How large does `tool_input` get?

Largest observed during a routine scaffolding session:

| Tool | Fields | Largest observed |
|---|---|---|
| Bash | `command`, `description` | `command` 1,024 B |
| Edit | `file_path`, `old_string`, `new_string`, `replace_all` | `new_string` 921 B, total 1,251 B |
| Write | `file_path`, `content` | `content` 2,165 B, total 2,396 B |

These are small only because this session wrote small files. `Write.content` is unbounded in
principle — a generated file of several hundred KB is an ordinary thing for an agent to produce.

Consequences for Telegram's 4,096-character limit:

- A Bash `command` fits on a card in the common case, but not always; truncation is mandatory, not
  optional.
- `Write` and `Edit` cannot be shown inline as a rule. Phase 1 only relays Bash, so this is deferred
  — but the `command_full` / `command_summary` split in `ApprovalRequest` already anticipates it,
  and the "show full content" affordance should be `sendDocument`, not a second message.
- Redaction must run over the *full* payload before truncation. Truncating first and masking second
  would leak whatever the truncation happened to preserve.

---

## Exit-code semantics (verified)

| Exit code | stdout | Observed result |
|---|---|---|
| 0 | valid decision JSON | Decision applied. |
| 0 | empty | Tool **runs** (no opinion). |
| 0 | malformed / not JSON | Tool **runs**. Garbage is treated as no opinion. |
| 1 | anything | Tool **runs**. stderr shown as a non-blocking hook error notice. |
| 2 | anything | Tool **blocked**. stderr shown, prefixed with `PreToolUse:Bash hook error:`. |

Exit 2 is the only failure mode that denies. Everything else lets the command through.

**This is the reason the bridge cannot be naively "fail closed by crashing."** A Python traceback
exits 1 and fails open. Therefore:

- `hook_bridge.py` wraps its entire body in a catch-all that prints a deny decision and exits 0.
- Because that still leaves the case where the interpreter never starts (bad shebang, missing
  Python, import error at startup — all exit 1), the hook should be invoked through a thin shell
  wrapper that maps *any* unexpected outcome from the Python process to an explicit deny, or failing
  that to exit 2. Fail-closed has to be enforced by the layer *outside* the thing that can crash.
- Exit 2 stays as the last resort. Note its output is visibly framed as a hook *error* rather than a
  policy decision, so the wrapper form with a clear reason is strictly better UX when reachable.

---

## Running more than one session at once

The navigator/driver workflow this project exists for means two Claude Code sessions in one
project directory at the same time. What that costs is mostly undocumented.

**Hook configuration is strictly per-project or per-user.** There is no `--settings` flag and no
environment variable pointing at an alternative settings file, so two sessions in one directory
necessarily share one hook configuration. That is fine here — both should route through the same
bridge — but it does mean a session cannot be given different hook behaviour at launch.

**Nothing in the payload says which session is the navigator.** `session_id` distinguishes them and
nothing else does; `agent_type` appears only for subagents. The role is known only to whoever
started the session, so it has to be declared there:

```bash
HALYARD_ROLE=navigator claude
HALYARD_ROLE=driver    claude
```

The docs say hooks run "with Claude Code's environment" and that "the hook process inherits the
parent environment", which were never spelled out for custom variables. Observed indirectly: a
Claude Code subprocess sees 46 variables including `SHELL`, `SHLVL`, `SSH_AUTH_SOCK` and
`__CFBundleIdentifier` — none of which Claude Code sets — so the launching shell's environment does
come through. Undocumented extras also present: `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID`. The
payload already carries `session_id`, so neither is needed.

### Measured

Two real Claude Code sessions were launched in this directory at the same time, one with
`HALYARD_ROLE=navigator` and one with `HALYARD_ROLE=driver`. The navigator ran a command carrying
the eight-second block marker; the driver ran an ordinary command a few seconds later. Observed:

```
22:19:13  navigator (pid 42858)  hook starts, blocks for 8s
22:19:20  driver    (pid 42944)  hook fires   ← inside the navigator's block
22:19:21  navigator (pid 42858)  block released
```

✅ **Hooks fire independently across sessions.** The driver's hook ran at the seventh second of the
navigator's eight-second block — they overlapped. There is no per-project serialization, so a
session blocked on an approval does not stall another session's tools. This was the finding that
could have forced a redesign of Phase 3; it did not.

✅ **A variable set at launch reaches the hook.** `role=navigator` and `role=driver` were logged
correctly by each session's hook, confirming that `HALYARD_ROLE=navigator claude` propagates. This
is the whole mechanism behind the navigator/driver split — the docs only say hooks inherit "Claude
Code's environment" without confirming it holds for custom variables, and now it is confirmed.

As a side effect, the passive behaviour of `observe.sh` was reconfirmed: on an unmarked command it
returns no opinion (exit 0, empty stdout) and Claude Code falls through to its own permission
prompt.

## Headless sessions

The docs confirm `SessionStart` and `Setup` hooks fire under `claude -p`, but say nothing about
`PreToolUse`. Measured by running a nested headless session from inside another one:

```bash
claude -p "Use the Bash tool to run exactly this one command and then stop: echo HEADLESSHOOKFIRED" \
  --allowedTools "Bash" --max-turns 4
```

✅ **`PreToolUse` fires in headless mode.** The log picked up an entry under a new `session_id`,
carrying the bare `echo HEADLESSHOOKFIRED` command. Also worth noting: `permission_mode` came
through as `default` despite `--allowedTools "Bash"` pre-approving the tool, so pre-approval does
not skip the hook.

This makes a headless-driven smoke test possible, but the automated end-to-end tests deliberately
do **not** use it. Driving a real Claude Code session costs tokens and needs working credentials on
every run, and the behaviour above is measured rather than documented — which is fine for a note
here and a poor foundation for a test suite. The tests feed the bridge synthetic payloads instead;
the "does Claude Code really honour the decision" link is proven by hand, once, and recorded above.

### Still to measure

| Question | Why it matters | Status |
|---|---|---|
| Is there a ceiling on a hook's `timeout`? | The approval window cannot exceed it, and exceeding it fails open. 600s is the default; no maximum is documented. If a large value is honoured, the window can be hours. | ☐ |

Only the timeout ceiling is left, and it does not block Phase 1 — it sets how long the approval
window can reasonably be, which is a defaults question, not a design one.

## Open questions raised during observation

- **`permission_mode: bypassPermissions`.** Does a PreToolUse hook still fire, and is its deny still
  honored? If the hook is skipped in that mode, the relay is silently bypassed — which is worth
  knowing before trusting it. Untested.
- **Subagent calls.** `agent_id` / `agent_type` were never observed. If subagent tool calls fire
  hooks too, a single session can produce concurrent approval requests, which the registry must
  handle. Untested.
- **Concurrency.** Claude Code runs independent tool calls in parallel; several were dispatched
  together during this session. Multiple hooks can therefore block simultaneously on the same
  session, and the control plane must key waiting futures by `tool_use_id`, not by `session_id`.
- **`ask` and `defer`.** Neither was exercised. `ask` may be a better response than `deny` for the
  unreachable-control-plane case, since it would fall back to the local terminal prompt rather than
  hard-failing — but only when a human is actually at the keyboard, which is precisely when this
  product is not in use. Left alone for now.
