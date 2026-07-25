# Antigravity Adapter Design Notes

## Overview
This document explores the feasibility of integrating Google Antigravity (AGY) with Halyard Fleet to allow managing AI agent permissions and steering sessions via Telegram. 

**Conclusion:** It is highly feasible. Antigravity shares a virtually identical hook-based architecture with Claude Code, making it a natural fit for Halyard's control plane.

## 1. Interception Mechanism (The Hook)

Antigravity provides a `PreToolUse` hook that fires before a tool call executes. Like Claude Code, it uses a standard I/O contract:
- **Input:** JSON payload via `stdin` containing tool call details and context.
- **Output:** JSON payload via `stdout` detailing the decision.

### Input Schema (Antigravity `PreToolUse`)
The agent passes context such as `toolCall` (which contains `name` and `args`), `stepIdx`, `conversationId`, `workspacePaths`, and `transcriptPath`.
```json
{
  "toolCall": {
    "name": "run_command",
    "args": {
      "CommandLine": "npm test",
      "Cwd": "/workspace/project"
    }
  },
  "conversationId": "ec33ebf9-...",
  "workspacePaths": ["/workspace/project"],
  "transcriptPath": "~/.gemini/antigravity/brain/ec33ebf9-.../.system_generated/logs/transcript.jsonl"
}
```

### Output Schema (Antigravity `PreToolUse`)
The bridge script must answer with:
```json
{
  "decision": "allow", // Can be "allow", "deny", "ask", or "force_ask"
  "reason": "Approved by user via Telegram"
}
```
*Note:* This maps perfectly to Halyard's existing approval semantics.

## 2. Integration Points

### The Bridge (`hook_bridge.py`)
The existing `hook_bridge.py` is deliberately stupid and minimal. To support Antigravity, the bridge simply needs to:
1. Parse the Antigravity `toolCall` schema.
2. Send the standard `POST /v1/approvals` request to Halyard Core.
3. Translate the Core's `allow`/`deny` response into Antigravity's expected JSON format.

Since the inputs from Claude Code and Antigravity differ slightly in field names (e.g., `tool` vs `toolCall`), the bridge can either sniff the payload to determine the runtime, or we can provide a dedicated `antigravity_bridge.py`.

### Bidirectional Communication (Telegram ↔ Antigravity)

**1. Antigravity -> Telegram (Output from Agent)**
You correctly assumed hooks would help retrieve the agent's responses! Antigravity supports a **`Stop`** hook that fires when the execution loop terminates. While it might not carry the text natively like Claude Code, the hook payload provides `transcriptPath`. Halyard can simply read the latest appended lines in this `.jsonl` file and send them back to the Telegram chat.

**2. Telegram -> Antigravity (Input from User)**
Sending messages from Telegram into Antigravity requires injecting them into the active `conversationId`. Since Antigravity sessions can run via IDE or standalone, Halyard can achieve this by:
- Using the **Antigravity Python SDK** (`agent.chat()`) if leasing programmatically.
- Using **Hooks** (`PreInvocation` / `PostInvocation`), which support an `injectSteps` schema allowing `{"userMessage": "A message from the user"}` to be pushed into the agent's thought loop. 
  *Note:* The `Stop` hook's `reason` field is strictly processed as a system message.

  **Corrected by measurement.** The two-step "wake it from `Stop`" plan does not
  work: `Stop` fires when a turn *ends*, not while the agent is idle — measured
  across a thirteen-minute silence, with no hook records at all — so when a
  message arrives at an idle conversation there is no invocation of `Stop`
  pending to answer with `{"decision": "continue"}`. A hook that is not being
  called cannot return anything.

  What was measured to work: hold the text in the control plane, wake the
  conversation with a short `agentapi send-message` doorbell, and answer the
  resulting `PreInvocation` with
  `{"injectSteps": [{"userMessage": "..."}]}`. The model obeyed an instruction
  present only in the injected step, and the step appears in neither
  `transcript.jsonl` nor `transcript_full.jsonl`. One short system line per
  message remains — the doorbell — and the person's own words arrive as a real
  user turn.
- Or, manually tailing transcripts and triggering a CLI command if Antigravity has a resume equivalent (`agy --resume <session_id>`).

### AgentRunner (The Adapter)
Halyard uses the `AgentRunner` protocol (`src/halyard/agents/base.py`) to inject messages back into live sessions and retrieve session metadata.
- **Session Identification:** Antigravity stores transcripts in `~/.gemini/antigravity/brain/<conversationId>/`. Halyard can parse the `.jsonl` transcripts to resolve session IDs and names (which addresses your need to specify the chat by name).
- **Injection:** The Halyard `AntigravityAdapter` will implement the `send` method using one of the injection mechanisms discussed above.

### Wiring (`halyard wire`)
To gate Antigravity, `halyard wire` will need to inject the hook definition into Antigravity's configuration. Antigravity supports scoped matchers, meaning we can restrict the gate specifically to high-risk tools like `run_command` or `write_to_file`.

```json
{
  "safety-gate": {
    "PreToolUse": [
      {
        "matcher": "run_command",
        "hooks": [
          {
            "command": "./bridge/hook.sh"
          }
        ]
      }
    ]
  }
}
```

## 3. Implementation Steps

1. **Adapter Creation:** Implement `AntigravityAdapter` conforming to the `AgentRunner` protocol in `src/halyard/agents/antigravity.py`.
2. **Bridge Update:** Update `hook_bridge.py` (or create a variant) to parse Antigravity's `stdin` and format the `stdout` response.
3. **Wiring Logic:** Add Antigravity config file parsing to `wiring.py` so `halyard wire` can merge the `PreToolUse` hook without destroying existing user settings.
4. **Testing:** Verify the timeout hierarchy (`approval deadline < bridge HTTP timeout < hook timeout`) behaves correctly with Antigravity's default hook timeout.
