# Antigravity hook behaviour

What was found before writing any adapter, and how each part was established.
Two sources, kept apart on purpose:

- **Measured** — read off this machine: the filesystem, a real conversation's
  transcript, the conversation database.
- **Documented** — read from Antigravity's own hooks page, saved locally as
  `hooks.html`. Believed, not proven. Nothing here has been through a live
  hook yet.

That distinction is the whole reason this file exists before any code. Both
Codex postmortems end in the same place: an assumption that a boundary was
shared when it was not.

## The finding that matters

**`bridge/hook.sh` would fail open under Antigravity.** Its hard-coded denial —
the one that catches a Python that will not start — is Claude Code's shape:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...}}
```

Antigravity's documented output is flat and uses a different key:

```json
{"decision": "deny", "reason": "..."}
```

An unrecognised payload is the case every runtime measured so far treats as *no
opinion*, and no opinion runs the command. So the wrapper that exists to make a
crash into a refusal would, under Antigravity, make a crash into an approval.

This has **not** been confirmed against a running Antigravity. It is the first
thing to measure, and until it is, no Antigravity project should be wired.

The bridge has the same problem in the other direction: it reads `tool_name`
and `tool_input.command`, and Antigravity sends `toolCall.name` and
`toolCall.args.CommandLine`. That failure is louder — the command arrives empty
or as JSON — but it is the same root: one shape assumed to be three runtimes'.

## PreToolUse — documented

Input on stdin:

```json
{
  "toolCall": {
    "name": "run_command",
    "args": {"CommandLine": "npm test", "Cwd": "/workspace/project", "WaitMsBeforeAsync": 5000}
  },
  "stepIdx": 19,
  "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
  "workspacePaths": ["/workspace/project"],
  "transcriptPath": "~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl",
  "artifactDirectoryPath": "~/.gemini/antigravity/brain/<id>"
}
```

Output on stdout:

```json
{"decision": "deny", "reason": "...", "permissionOverrides": ["command(npm test)"]}
```

`decision` is documented as **required**, with four values:

| | |
|---|---|
| `allow` | runs it, no prompt |
| `deny` | hard block |
| `ask` | prompts, but respects the user's "Always Allow" |
| `force_ask` | always prompts, ignoring cached permissions |

Two of these are better than anything the other runtimes offer.

`force_ask` is what `/pause` has always wanted to mean. On Claude Code, pausing
returns no opinion and the runtime's own allow-list then runs matching commands
with no prompt at all — documented in the README because it surprises people.
Under Antigravity a paused gate could return `force_ask` and be genuinely
certain a human sees it.

And `deny` is a real refusal rather than an absence, which is what makes the
fail-closed contract expressible here at all.

## Events — documented

| Event | Matcher |
|---|---|
| `PreToolUse` | tool name, as a regular expression |
| `PostToolUse` | tool name |
| `PreInvocation` | ignored |
| `PostInvocation` | ignored |
| `Stop` | ignored |

Matchers are regular expressions over the tool name: `""` or `"*"` for
everything, `"run_command"`, `"run_command|view_file"`, `"browser_.*"`.

The tool to gate is `run_command`. Note that this is a third spelling of the
same idea — Claude Code says `Bash`, Codex says `Bash` from its CLI and `exec`
from its app, Antigravity says `run_command`. The matcher has been wrong once
already for exactly this reason.

## Configuration — documented

`hooks.json`, in `.agents/` inside a workspace or in `~/.gemini/config/`:

```json
{
  "safety-gate": {
    "enabled": false,
    "PreToolUse": [
      {"matcher": "run_command", "hooks": [{"command": "./scripts/safety-check.sh"}]}
    ]
  }
}
```

**The file is keyed by hook name, not by event.** Claude Code and Codex both
put events at the top level; Antigravity puts a named group there and the
events inside it. `halyard wire` merges structurally, so this is a third shape
to teach it rather than a variation of the two it knows.

The named group is an improvement worth taking: Halyard's hooks can live under
one key, and `unwire` can remove that key instead of matching command paths.

**`enabled: false` is a new way for a gate to be silently absent.** A hook can
be present, correct, pointing at the right script, and switched off. Codex's
untrusted-hook state cost an evening; this is the same shape and `doctor` should
check it from the start rather than after it happens.

## What is on this machine — measured

Antigravity is installed at `/Applications/Antigravity.app`, with real
conversations under `~/.gemini/antigravity/`.

```
~/.gemini/config/config.json                      no hooks key today
~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl
~/.gemini/antigravity/conversations/<id>.db       SQLite
~/.gemini/antigravity/bin/agentapi                a two-line shim into the app
```

There is **no `agy` or `antigravity` command on PATH.** `bin/agentapi` is a
two-line shim into `Antigravity.app/Contents/Resources/bin/language_server`, and
it offers exactly the three things an adapter needs — measured by running it:

```
get-conversation-metadata <conversation_id>
new-conversation [--model=<flash_lite|flash|pro>] [--title=<title>] [--profile=<profile>] <prompt>
send-message [--title=<title>] <recipient_id> <content>
```

`send-message` is a candidate for delivery, `--title` for the stable name seats
are addressed by, and `get-conversation-metadata` for everything the transcript
does not carry. `--model` also gives `options()` its three values.

**It talks to a running application, not to the filesystem.** Called with the
app closed it answers:

```json
{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}
```

So this is an IPC client, and how the address is published is **not measured** —
with the app shut, nothing under `~/.gemini` carries a socket, a port, or a
lock naming one. It is presumably set for processes the app starts, which is
not what a control plane running beside it is.

That is a real constraint rather than a detail. Claude Code and Codex are both
driven by a CLI that works whether or not an application is open; Antigravity's
appears to need one running. If that holds, an Antigravity seat can only be
written to while its app is up — worth knowing before promising the second half
of the product.

### The transcript does not identify a session

Measured on a real conversation. Record types are `USER_INPUT`,
`PLANNER_RESPONSE`, `RUN_COMMAND`, `VIEW_FILE`, `CHECKPOINT` and others; the
fields present across all of them are:

```
content  created_at  source  status  step_index  thinking  tool_calls
truncated_fields  type
```

**No working directory, no session name, no model.** Claude Code has all three
in its transcript and Codex has two of them; the design note's assumption that
names could be read from these transcripts does not hold.

The working directory *is* recoverable — it sits in the protobuf blob in
`conversations/<id>.db`, and reading a real one produced
`/Users/jammer/Documents/dev/ai/halyard-fleet` correctly. That is not a source
to build on: the Codex postmortem already refused `state_5.sqlite` for
announcing its own schema version, and an undocumented protobuf blob inside a
database is worse.

The payload solves this anyway. `workspacePaths` and `conversationId` arrive
with every hook call, so a card can be placed without reading anything. What is
missing is a **stable human-chosen name** — the thing seats are addressed by.
Nothing found so far provides one.

## Open questions, in the order they block work

1. **Does an unrecognised output really fail open?** Everything above rests on
   it. Measure with a hook that prints Claude-shaped JSON and see whether the
   command runs.
2. **Does `deny` actually stop a command**, and does a non-zero exit or empty
   stdout run it? The same table that was built for the other two runtimes.
3. **How does `agentapi` find the running app?** `send-message` is the whole
   of delivery and it needs `ANTIGRAVITY_LS_ADDRESS`. Start the app and look:
   the environment of the running process, a socket, or something published
   under `~/.gemini`. If only child processes of the app get it, delivery needs
   a different route.
4. **Does `--title` produce a name that survives, and does
   `get-conversation-metadata` return it?** Seats are addressed by name, and the
   transcript has none. `conversationId` works as a fallback and is at least
   stable, unlike a Claude Code `session_id`.
5. **What is the default hook timeout?** The ordering `approval < bridge < hook`
   is enforced at startup and has to hold here too.

## What not to do yet

Do not wire an Antigravity project. Until question 1 is answered, the wrapper
that makes every failure a refusal may be making them approvals, and that is the
one property this project exists to provide.
