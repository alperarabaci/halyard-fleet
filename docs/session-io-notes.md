# Reading from and writing to a live session

Phase 2 needs two things Phase 1 did not: an agent's output has to reach a channel, and a message
typed in that channel has to reach the agent. The design document flagged the second as the risky
one — *writing into an existing interactive TTY session is not reliable* — so this was measured
before anything was built, the way `hook-payload-notes.md` was.

- **Claude Code version observed:** 2.x
- **Measured on:** 2026-07-21
- **Method:** nested headless sessions (`claude -p`) driven from another session, with a passive
  hook wired to `Stop`, `Notification` and `SessionEnd`

---

## The three findings that shape Phase 2

1. **The `Stop` hook carries the assistant's text.** `last_assistant_message` is a plain string
   holding the final assistant message of the turn. Output can be relayed through exactly the
   mechanism Phase 1 already uses — a hook, a bridge, an HTTP call — with no transcript parsing.
2. **`--resume` continues the same session.** Same `session_id`, same transcript, context intact.
   This is how a message gets *in*; there is no bidirectional stream to write to.
3. **Two writes that overlap in time fork silently.** Two resumes of one session at the same
   moment both succeed, both append, and neither sees the other's turn. Nothing errors, nothing
   is corrupted, and the conversation quietly becomes two threads in one file.

The third was first read as "so nothing may write into a session somebody has open", and that
reading was wrong — see the correction below. Overlapping writes fork; one writer at a time is
fine, including into a session the desktop app is holding.

---

## Reading a session

### The Stop hook

Fires once per turn. Observed payload keys:

```
last_assistant_message   stop_hook_active   session_id   transcript_path
prompt_id   cwd   permission_mode   effort   background_tasks   session_crons
```

`last_assistant_message` was a `str` carrying the exact reply — `'noted'` and `'8412'` in the two
measured turns — with `stop_hook_active: False`.

This is the output path. It needs no new machinery: a `Stop` hook posting to the control plane is
the same shape as the approval bridge, and the same fail-closed reasoning does *not* apply, because
failing to relay a message is a lost message rather than an unsupervised command.

### The transcript is not the interface

The transcript at `transcript_path` is clean, append-only JSONL — measured at 998 lines with zero
unparseable ones, records chained by `parentUuid`, assistant content split into `text`,
`thinking` and `tool_use` blocks.

**It is still the wrong thing to build on.** The documentation is explicit that the entry format is
internal and changes between versions, so anything parsing it breaks on a release. The measurement
above says it would work *today*; the documentation says it will not keep working. The `Stop` hook
gives the same text with a supported contract, so there is no reason to take the risk.

Recorded here only so nobody re-derives it and reaches the opposite conclusion.

### Other hooks worth knowing about

`SessionEnd` fired with `reason: other` for headless runs. `Notification` — which carries a
`notification_type` including `agent_needs_input` and `agent_completed` — did **not** fire during
headless runs with no permission prompts, so it is untested here and is the likely mechanism for
"the agent is waiting on you" in an interactive session.

---

## Writing to a session

There is no input stream. `--output-format stream-json` is output-only; there is no documented
`--input-format`, and no way to feed a message into an already-running process. The way in is to
start a new turn against the existing session:

```bash
claude -p --resume <session_id> "the user's message" --output-format json
```

Measured: session `5bb486b8…` was told to remember the number 8412 in one turn, then resumed in a
separate process and asked for it back. It answered `8412`, and the returned `session_id` was
unchanged. Context carries; the session is continued rather than forked.

**Cost scales with conversation length.** Each resumed turn reported ~24,900 cached input tokens
for a session holding only a handful of messages, because every turn replays the whole
conversation. A navigator session that has been running for hours will re-read all of it on every
message relayed from a chat. That is a per-message cost, and it is the argument for keeping the
driver lean and compacting it often — which is how this project is already used.

**Practical detail:** a backgrounded `claude -p` warns `no stdin data received in 3s` and prints it
*before* the JSON, which breaks a naive parse. Redirect `< /dev/null`.

---

## Concurrent access: the hazard that decides the design

Two `--resume` calls against one session, launched at the same instant:

| | Result |
|---|---|
| Both processes | Succeeded — `subtype: success`, one answered `ALPHA`, the other `BETA` |
| `session_id` | Identical for both |
| Transcript | Grew from 15 to 27 lines, **zero unparseable** |
| Context each saw | `cache_read_input_tokens: 24861` for **both** — the same pre-fork state |

Nothing failed. No lock, no warning, no error. But the second turn did not see the first: both
answered from the same history, and both appended. The documentation describes the same thing for
the interactive case — *"if you resume the same session in two terminals without forking, messages
from both interleave into one transcript."*

**One of the two turns then disappeared.** Asked afterwards to list everything it had been asked,
the session answered:

```
- Remember the number 8412 and reply "noted"
- Recall the number, digits only
- Reply with only "BETA"
- List every distinct thing asked in this conversation, in order
```

`ALPHA` is not there. Both turns succeeded, both were written to the transcript, and only one
lineage survived into the conversation the next resume sees. The other was orphaned.

So the failure mode is not a crash. It is a turn that reported success and then quietly stopped
being part of the conversation — which is worse, because nothing reports it and the transcript
still parses cleanly.

### Continuity, when there is only one writer

The same test proves the property Phase 2 depends on. Four turns issued from four separate
processes were all remembered as one conversation, in order. A message relayed from a chat is not a
side channel: it lands in the session itself, and someone resuming that session in a terminal
afterwards sees it in the history like any other turn.

That is what makes handoff work, and it holds exactly as long as one writer owns the session.

### What this forces, and what it does not

The narrow reading of the above — *therefore nothing may write into a session somebody has open* —
was wrong, and it cost two days of building around a wall that was not there.

**Measured afterwards:** a message typed in Telegram was delivered with `claude -p --resume` into a
session the desktop app had open, and the app picked the turn up and displayed it. Writing into a
live session works. The documentation says concurrent access is not *documented* as supported,
which is a different claim from not working, and a measurement outranks a silence.

What the fork measurement actually establishes is narrower: **two writes that overlap in time**
diverge. One writer at a time is fine, whoever it is. So:

- Sends are serialised per session in the runner, so Halyard never races itself.
- The case left is a person typing at the desk in the same moment a message arrives from the
  channel. That is a real hazard and a rare one — being away is what makes somebody use the phone.

No ownership model, no handoff protocol, no registry field recording how a session was born. A
session is addressable by name, and messages go into it.

### Running it from the right directory

`claude --resume <id>` looks for the conversation **inside the current project**. Run it from
anywhere else and it answers `No conversation found with session ID`, with the transcript sitting
on disk the whole time. A control plane runs from its own repository, so it has to change directory
into the session's before resuming.

The directory comes from the `cwd` a transcript record carries, not from the encoded folder name
transcripts are filed under: that encoding replaces path separators with dashes and cannot be
reversed, since a real dash in a folder name looks identical.

---

## SessionEnd is a reliable handoff signal

Handing a session to Halyard cannot rest on the user saying the terminal is closed. Get that wrong
and the session forks exactly as above, silently. So the question is whether Claude Code reports a
session ending in a way that can be *observed* rather than asserted.

Measured across five interactive sessions closed three different ways:

| Exit | `reason` | Fired? |
|---|---|---|
| `/exit` or Ctrl+D at the prompt | `prompt_input_exit` | ✅ |
| Ctrl+C | `other` | ✅ |
| Terminal window closed outright | `other` | ✅ |

**Every terminated session produced a `SessionEnd`**, including the one whose window was closed
without exiting. A session still running produced none, which is the other half of what makes the
signal usable.

So ownership can be taken on an observed `SessionEnd` rather than on a claim. "Take over" from the
channel means *close the terminal and I will pick it up when I see it end* — the Phase 2 equivalent
of failing closed: an unobserved handoff is not a handoff.

A timeout fallback is still worth having for the case where the machine loses power mid-session and
nothing gets to fire, but it is a backstop rather than the mechanism.

### The Stop hook works in interactive sessions too

Confirmed separately: an interactive session produced a `Stop` payload carrying a 1,100-character
`last_assistant_message`. The output relay behaves the same whether the session is driven from a
terminal or headless, so watching a session does not depend on who owns it.

---

## Still to measure

| Question | Why it matters |
|---|---|
| Does `Notification` fire with `agent_needs_input` in an interactive session, and what is in it? | It is the natural "the agent is waiting on you" signal for a channel. |
| Does `Stop` fire for subagent turns, or only `SubagentStop`? | Decides whether subagent chatter floods the channel. |
| Is there any signal when a session is *started*, so Halyard learns about one before it asks for anything? | `SessionStart` exists and is untested here; it would let the registry know a session before its first approval. |
