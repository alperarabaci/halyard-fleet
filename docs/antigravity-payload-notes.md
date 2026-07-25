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

## The finding that matters — measured

Antigravity's documented output is flat:

```json
{"decision": "deny", "reason": "..."}
```

Halyard's `bridge/hook.sh` prints Claude Code's nested shape instead. **It is
not understood.** A hook that printed a Claude-shaped denial did not stop the
command: Antigravity fell through to its own permission prompt, and once that
was answered the command ran and completed successfully.

```
hook fired (witness present), printed permissionDecision: "deny"
RUN_COMMAND  created 22:22:40  completed 22:25:04  "The command completed successfully."
```

The two-and-a-half minute gap is a human answering a prompt, not a slow turn.

So the concern this file opened with is real. `hook.sh`'s hard-coded denial —
the one that exists to catch a Python that will not start — does not deny here.
What saves it from being a silent approval is Antigravity's own prompt, and
that prompt respects the user's cached "Always Allow" decisions: a second,
identical command ran four seconds later without asking anybody. So for any
command a user has ever waved through, an unreadable hook answer is an
approval.

**Nothing may be wired for Antigravity until the bridge emits `decision`.**

### How this was got wrong first

Worth keeping, because the mistake is the one this project keeps making.

The deny run was checked ninety seconds after the message was sent. The marker
file was absent, and absence was read as the gate working. It was not: the
approval was still sitting unanswered, and the command ran two minutes later.
Then an allow run completed in four seconds — cached permission from the first
approval — and the pair was read as "deny blocks, allow passes, therefore the
shape is understood."

Two controls would have caught it. A witness file proves a hook *ran* and says
nothing about whether its answer was *understood*; that needs the runtime's own
record of what happened to the command, which the transcript had all along. And
a repeated command is not an independent trial once a permission can be cached.

## Timing

A turn driven through `send-message` takes seconds when nothing blocks and
minutes when a human is asked. Any conformance check here has to distinguish
"stopped" from "still waiting", and reading the transcript's `RUN_COMMAND`
record — which carries both created and completed times, and whether it
succeeded — is how.

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

**A matcher of "ignored" also means a different shape.** The two tool events
wrap their handlers in a `matcher`/`hooks` group; the other three are a *flat*
list of handler objects:

```json
"PreToolUse": [{"matcher": "run_command", "hooks": [{"command": "./hook.sh"}]}],
"Stop":       [{"command": "./relay.py"}]
```

Not cosmetic. A `Stop` written in the grouped shape puts no `command` where
Antigravity looks for one, so the relay never runs and no reply reaches a phone
— with the file present, the path correct, and everything reporting wired.

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

`bin/agentapi` is a two-line shim into
`Antigravity.app/Contents/Resources/bin/language_server`, and it offers exactly
the three things an adapter needs — measured by running it:

```
get-conversation-metadata <conversation_id>
new-conversation [--model=<flash_lite|flash|pro>] [--title=<title>] [--profile=<profile>] <prompt>
send-message [--title=<title>] <recipient_id> <content>
```

`send-message` is a candidate for delivery, `--title` for the stable name seats
are addressed by, and `get-conversation-metadata` for everything the transcript
does not carry. `--model` also gives `options()` its three values.

**It talks to a running application, not to the filesystem** — measured. With
the app closed it answers `{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}`. With
it open, two variables are needed and neither is published anywhere on disk:

```
ANTIGRAVITY_LS_ADDRESS=127.0.0.1:60762
ANTIGRAVITY_CSRF_TOKEN=<the --csrf_token argument of the running language_server>
```

Both come off the running process. The port is one of two the `language_server`
listens on — the other answers `error reading server preface`, so it is not the
gRPC one — and the token is an argument on its command line. Discovering a
service by reading another process's arguments is not a contract; it is what is
available, and it will break without warning.

## A delivered message cannot be made to look like a user's

`agentapi send-message` is an agent-to-agent notification channel — its own
help says "Send messages to another conversation or yourself" — so Antigravity
files every delivery as a `SYSTEM_MESSAGE` and prefixes it with a sentence
saying the user did not send it. In the application it is drawn under a
**Message from System** header rather than as a turn the person typed.

`--title` is the only flag it takes, and it changes none of that. Measured with
`--title="alper (Telegram)"`, the envelope was byte-for-byte the shape it
always is:

```
[Message] timestamp=2026-07-25T00:38:11Z sender=system priority=MESSAGE_PRIORITY_HIGH content=...
```

`sender=system` is fixed, and `agentapi` has exactly three commands, so there
is no other way in. The other two runtimes deliver a genuine user turn; this
one has no interface that can.

### But `PreInvocation` can inject one — measured

`PreInvocation` is the one hook that answers with `injectSteps`, and
`{"userMessage": "..."}` is a supported step. Measured with a one-shot probe
wired beside the gate under its own hook name:

```
probe received: artifactDirectoryPath conversationId initialNumSteps
                invocationNum modelName transcriptPath workspacePaths
probe answered: {"injectSteps": [{"userMessage": "...tek kelimeyle onayla."}]}
the model said: "Onaylandı."
```

One word, because the instruction to answer in one word existed **only** in the
injected step. So the injection reaches the model, and it arrives as a turn the
person typed rather than as a system notice.

Two things follow, and both shape the adapter:

**It is written to neither transcript file.** Not `transcript.jsonl`, not
`transcript_full.jsonl` — grepped for the probe text in both, zero hits. The
message reaches the model and the screen and leaves nothing on disk, so
delivery cannot be confirmed by reading afterwards. The queue is therefore
emptied by the reader: `PreInvocation` fires before *every* model call, and a
queue nothing cleared would put one sentence into every step of the turn.

**An idle conversation still has to be woken.** `Stop` fires when a turn ends,
not while nothing is happening — measured across a thirteen-minute silence with
no records at all. A hook that is not being invoked cannot be answered, so
"return `continue` from `Stop` when a message arrives" has nothing to return
from. Waking still goes through `agentapi send-message`, which is why one short
system line per message remains: it is a doorbell, not the message.

## There are two Antigravities, and they share nothing

Installing the `agy` CLI adds a **second store**, not a second way into the
first:

```
~/.gemini/antigravity/        the application:  brain/, conversations/, annotations/
~/.gemini/antigravity-cli/    agy:              its own brain/, conversations/, ...
```

A conversation started in one is invisible in the other. Measured from the
symptom: `agy` was run inside this repository, and the session never appeared in
the application. Asked about it, `agy` itself answered that the two "share the
underlying session store" and to switch the app to the matching project folder.
Both halves of that are wrong — the directory listing above is the whole
argument.

**`--conversation <id>` does not resume.** `agy --help` says "Resume a previous
conversation by ID". Measured three times — with the value as a separate
argument, with `=`, and via `-c`, which is not read as a flag at all — it starts
a *new* conversation seeded with a summary of the one named. The databases are
the proof:

```
3792ab62… (named on the command line)   13 steps before, 13 after, marker absent
752d014d… (created by the call)          4 steps, and parent_references empty
```

Empty in both, so the two are not even linked; `conversation_summaries.db`
beside them is what the flag actually reads.

So **a conversation the CLI owns cannot be delivered to**, and the adapter
refuses one by name rather than trying. This is the more important half of the
finding: the attempt *succeeds*. It exits 0, and the text is answered in a
conversation nobody is watching while the seat somebody is watching sits there
looking idle. A visible failure is the better of the two, and `doctor` says so
for a seat that names a CLI conversation — findable and unreachable is the
combination worth printing.

**`send-message` genuinely drives a turn.** Measured: the message arrives in the
transcript as a `SYSTEM_MESSAGE` and the agent acts on it, producing a real
`RUN_COMMAND` a few seconds later. That is `AgentRunner.send` for this runtime,
and unlike Claude Code and Codex it needs the application running.

**A sandbox sits between the gate and execution.** The first probe asked for
`touch /tmp/...` and the transcript recorded:

```
The command failed with exit code: 1
touch: /tmp/agy-gate-probe.marker: Operation not permitted
```

So there are now three separate things that can stop a command here — the gate,
the runtime's own permission flow, and the sandbox — and a conformance check
that cannot tell them apart proves nothing. `halyard verify` already learned
this once against Codex; it will need a writable path inside the workspace and a
witness file for Antigravity too.

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

1. **Does the documented shape actually block?** `{"decision": "deny"}` has
   not been tried — only Claude's shape, which does not. Everything else waits
   on this: if the documented shape blocks cleanly, the bridge needs a
   translation and nothing more.
2. **What do the failure modes do?** A non-zero exit, empty stdout, malformed
   output, a missing interpreter and a hook that outruns its timeout — the same
   table built for the other two runtimes, and the one that decides whether a
   wrapper can hold the line here at all. Judge each by the transcript's
   `RUN_COMMAND` record, never by a marker file alone.
4. **How does `agentapi` find the running app?** `send-message` is the whole
   of delivery and it needs `ANTIGRAVITY_LS_ADDRESS`. Start the app and look:
   the environment of the running process, a socket, or something published
   under `~/.gemini`. If only child processes of the app get it, delivery needs
   a different route.
5. **Does `--title` produce a name that survives, and does
   `get-conversation-metadata` return it?** Seats are addressed by name, and the
   transcript has none. `conversationId` works as a fallback and is at least
   stable, unlike a Claude Code `session_id`.
6. **What is the default hook timeout?** The ordering `approval < bridge < hook`
   is enforced at startup and has to hold here too.

## What not to do yet

Do not wire an Antigravity project. Until question 1 is answered, the wrapper
that makes every failure a refusal may be making them approvals, and that is the
one property this project exists to provide.
