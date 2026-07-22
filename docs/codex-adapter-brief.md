# Brief: what a Codex adapter would have to be

This is a question, not a specification. It is written for whoever investigates
adding Codex as a second agent runtime to Halyard Fleet — read this repository
first, then answer it.

**Answer with measurements, not with documentation.** Every load-bearing fact in
this project was wrong the first time it was read and right only after it was
run. Three hook exit-code behaviours, whether a paused gate asks or waives,
whether a session can be written into at all, whether a parent directory's
settings apply — each one cost hours because someone trusted a document.
`docs/hook-payload-notes.md` records those, including a correction written two
days late. Do not add to that file's genre.

If a question below cannot be answered by running something, say so explicitly
and say what you would need. An honest "unknown" is worth more here than a
confident paragraph.

---

## What Halyard is, in three sentences

It relays an agent's permission requests to a phone and blocks until a human
answers, failing closed on every error path. It relays the agent's output back
out, and delivers messages typed on the phone into the running session. It
currently speaks Claude Code and is written so that the second runtime does not
require rewriting the first.

## The two contracts a runtime has to satisfy

### 1. The gate

Halyard must be able to stop a command *before it runs* and answer allow or
deny, with a human in the loop, taking up to five minutes to reply.

In Claude Code this is a `PreToolUse` hook: a subprocess given the tool call on
stdin, whose stdout decides the outcome, and which is allowed to block. The
bridge posts this to the control plane:

```
POST /v1/approvals
{ session_id, agent_id, tool, command, tool_use_id, cwd, project_dir,
  role, session_name }
→ { decision: allow | deny | defer, reason, request_id, risk }
```

**`defer` means "no opinion"** — Halyard is paused and the runtime should decide
as though nothing were installed.

### 2. Sending a message into a live session

```python
async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool
```

The message must land *in the session itself*, so that whoever opens that
conversation later sees it in the history like any other turn. A bot that keeps
its own separate thread is not this. In Claude Code it is
`claude -p --resume <id> "<text>"`, run with the session's own directory as cwd.

The full protocol is `src/halyard/agents/base.py`:

```python
id            -> str
options()     -> {name: (values, enforced)}   # what /model and /effort accept
busy(sid)     -> bool                          # only turns this runner started
preferences(sid) -> (model, effort)            # what will actually be used
set_model(sid, m) / set_effort(sid, e)
send(sid, text, cwd) -> bool                   # must not raise
```

Output relay is a separate, weaker path: a post-turn hook that POSTs to
`/v1/messages` and swallows every error, because a lost chat message is not
worth interrupting a session over.

---

## The questions

**1. Does Codex have a blocking pre-execution hook at all?**
If it does not, the gate cannot exist in its current form and everything else in
this brief is moot — say that plainly and early rather than designing around it.
If something adjacent exists (an approval callback, an MCP middleware, a policy
hook, a sandbox escape point), name it and show it running.

**2. What does Codex do with a hook that fails?**
Measure all of it, one case at a time: a non-zero exit, an empty stdout,
malformed output, a hook that exceeds its timeout, an interpreter that never
starts. For each, does the command **run** or **stop**?

This is the single most important answer in the document. Claude Code runs the
command in every one of those cases except one specific exit code, which is why
the fail-closed guarantee lives in a nine-line shell wrapper rather than in the
Python it calls. A runtime that fails *closed* by default would let the bridge
be much simpler; a runtime that fails open needs the same trick. Do not guess
which one this is.

**3. Can a session be resumed non-interactively, and does it stay the same
session?**
Send a message into an existing conversation from a separate process. Then
verify from *inside* that conversation that it is there — ask the session to
recall something only that message contained. A new conversation that answers
correctly is not the same as the message having landed.

**4. What happens when two writes to one session overlap?**
Run two resumes of the same session concurrently and inspect the history
afterwards. In Claude Code neither call fails: they fork silently, and one
lineage is simply absent. Nothing errors, and the transcript still parses. If
Codex does the same, sends have to be serialised per session; if it locks or
refuses, that is worth knowing because it removes a whole class of hazard.

**5. What identifies a session across a restart?**
Halyard is addressed by a stable, human-typed name — a Claude Code `session_id`
is a fresh UUID on every restart, so it cannot be the key. Is there a name, a
title, a workspace id? Where does Codex keep its transcripts, and what in them
survives a restart?

**6. What can be chosen per turn?**
Models, reasoning effort, anything else. `options()` exists so a runtime answers
this for itself rather than the chat layer holding a list. Also: **what does
Codex use when nothing is specified?** Claude Code's headless default is its
cheapest model, which quietly downgraded every message sent from a phone until
it was measured.

**7. What is the equivalent of a per-project settings file, and who else writes
to it?**
Claude Code's `settings.local.json` also holds the user's accumulated
permission rules. Our own setup instructions told people to overwrite that file,
which silently deleted those rules; `halyard wire` exists because of it. If
Codex has a config file Halyard would edit, find out what else lives in it
before proposing to write to it.

---

## Constraints that are not up for negotiation

- **Fail closed.** No error path, timeout, crash, or unreachable service may
  result in a command running. If Codex cannot express refusal, that is a
  finding, not a problem to route around.
- **No automatic approval**, no risk threshold above which things pass, no model
  deciding permissions on a human's behalf.
- **The bridge stays stupid.** It reads stdin, makes one HTTP call, translates
  the reply. Policy, risk classification, and redaction live in `core/`, where
  they are tested. Do not put logic in the thing that runs as a subprocess.
- **No second protocol.** If the existing `AgentRunner` shape does not fit
  Codex, propose the change to `AgentRunner` — do not add a parallel one.
- **English only**, throughout code, comments, and documentation.

## What to deliver

1. A written answer to each of the seven questions, marking each one **measured**
   or **unknown**, with the command you ran and what it printed.
2. Whether the gate is possible at all, stated in the first paragraph.
3. The smallest change to `AgentRunner` that would fit both runtimes — or
   confirmation that none is needed.
4. What you would build first to prove it end to end, in one paragraph.

Do not write the adapter yet. The design follows the measurements, and this
project has already paid twice for doing that in the other order.
