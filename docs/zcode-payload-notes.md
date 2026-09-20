# ZCode hooks — measured

**Version:** ZCode 3.11.2 (`/Applications/ZCode.app`), macOS · **Measured:** 2026-09-15 ·
**Calls captured:** 15, from a probe workspace whose hooks wrote down every one ·
**Status:** the gate (`PreToolUse`) and the reply relay (`Stop`) are wired, a session's
title is read from ZCode's own database, and a message goes into a session over the
engine's own protocol — see [Delivery](#delivery-measured-2026-09-19-and-2026-09-20-engine-0165),
measured on engine 0.16.5.

Each section says whether it was **measured** here or only **documented** by ZCode's
own guide — the built-in `zcode-guide` plugin's `diagnosing-hooks` and
`zcode-configuration-guide` skills. One documented claim turned out to be wrong, and
it is the one that decides whether anything runs.

## In short

- The payload is Claude Code's, with camelCase copies of every field beside the
  snake_case ones and a few of ZCode's own.
- Hooks live in the workspace's `.zcode/config.json` under `hooks.events`, and none
  run without `hooks.enabled: true`.
- **Workspace hooks wait for trust**, one declaration at a time. The guide says they
  do not. They do.
- A `PreToolUse` `allow` replaces ZCode's own approval prompt; a `deny` blocks and the
  reason reaches the agent.
- A call carries no session name, but ZCode keeps every session's title in its own
  database, under the same `session_id`.
- A message can be delivered from outside, by starting the engine as the application
  does and answering what it asks — including every permission, which is the gate.

## Where hooks are read (documented, then measured)

```json
{
  "hooks": {
    "enabled": true,
    "timeoutMs": 60000,
    "events": {
      "PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "process", "command": "/usr/bin/env", "args": ["…"], "timeoutMs": 600000}]}
      ]
    }
  }
}
```

In the workspace's `.zcode/config.json` (or `zcode.json`), or for every workspace in
`~/.zcode/cli/config.json`. The events sit under `hooks.events` — not directly under
`hooks`, which is the shape a plugin's own `hooks.json` uses and a configuration file
does not. Seven events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Stop`. The matcher is a
case-sensitive regular expression; `ApplyPatch` is matched as `Write` and `Edit`.

A `command` hook runs through a shell with `timeout` in seconds; a `process` hook runs
`command` with `args` and `timeoutMs` in milliseconds. The default is **60 seconds**,
shorter than an approval can wait. Output is JSON against a strict schema — one extra
key fails it — or an exit code: 0 passes, 2 blocks.

## Trust (measured)

With hooks written and switched on, nothing fired. ZCode's log said
`Project hooks pending workspace trust`, and every hook was `pending_trust`. They ran
once trusted, and not before. The guide's "workspace hooks have no trust gate" is wrong
for 3.11.2.

- Trust is per hook declaration, by SHA-256. A changed declaration — command, timeout,
  the switch — is a new one and waits again.
- The script a hook runs is not part of it. The probe's script was edited under a
  trusted hook and stayed trusted, unlike Codex, which hashes the handler.
- The application's hook list shows only the workspace that is open.

ZCode's engine answers for itself, run the way the application runs it:

```bash
ELECTRON_RUN_AS_NODE=1 /Applications/ZCode.app/Contents/MacOS/ZCode \
  /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  hooks trust status --workspace <project> --json
```

`review` gives the same list; `grant --workspace <p> --hook-digest <sha256> …` (or
`--all-current --bundle-digest <sha256>`) trusts, persistently (`trusted_persistent`);
`revoke … --all` takes it back. States: `pending_trust`, `trusted_persistent`,
`stale_digest`, `revoked`, `blocked_untrusted`, `blocked_policy`, `not_applicable`.

## What a call carries (measured)

Every event: `session_id` (`sess_<uuid>`, new with every task), `cwd`,
`hook_event_name`, `permission_mode` (`build`), `transcript_path`, and camelCase copies
(`sessionId`, `hookEventName`, `transcriptPath`, …) with `turnId`, `traceId`,
`timestamp`. The environment has `ZCODE_PROJECT_DIR`, `CLAUDE_PROJECT_DIR` and
`CLAUDE_SESSION_ID`.

| Event | What is worth knowing |
|---|---|
| `SessionStart` | `source: startup`, `model` |
| `UserPromptSubmit` | `prompt` |
| `PreToolUse` | every tool; `tool_name`, `tool_input`, `tool_use_id` (`call_<hex>`), plus `riskLevel`, `sideEffectScope` |
| `PermissionRequest` | only for a tool with side effects: a `Write`, not `ls`; `reason`, `requestId` |
| `PostToolUse` | `tool_response`; a command that exits non-zero comes here with `status: "failed"`, not as `PostToolUseFailure` |
| `Stop` | the whole reply in `last_assistant_message` |

Tools seen: `Bash` with `{command, description}`, and `Write` with
`{file_path, content}`. No session name appears in a call — see *Session titles*.

Two things to handle rather than trust:

- `transcript_path` is a fresh temporary file per call
  (`$TMPDIR/zcode-claude-hook-*/transcript.jsonl`), in no runtime's home.
- `cwd` changed within one session, between `/private/tmp/…` and `/tmp/…`.

## Decisions (measured)

The bridge's own output, returned by a probe:

| Returned | What happened |
|---|---|
| `PreToolUse` deny, with a reason | the command did not run; the agent was told the reason |
| `PreToolUse` allow | the `Write` ran 87 ms later, and no `PermissionRequest` or prompt appeared |
| exit 0, no output | ZCode's own flow: the `Write` raised a `PermissionRequest` and waited 17 s for the desk |

So an approval from the phone takes the place of the one at the desk, and silence is
"no opinion", which is what `hook.sh` turns a pause into.

## Session titles (found by ZCode, schema read here)

Not in any call, but not nowhere. Asked where the title a session shows in its list
comes from, ZCode found it in its own database, and the schema was read here the same
day (2026-09-16):

- `~/.zcode/cli/db/db.sqlite`, table `session`. `id` is the call's `session_id`
  (`sess_<uuid>`).
- `title`, and `title_source` saying where it came from: `first_input` — the first
  thing somebody typed, shown until there is a title — then `generated`, or `custom`
  when a person set it.
- `parent_id` ties a subagent's session to the one that started it, `directory` is
  where it works, and `time_archived` is set once it is put away.

The bridge reads the title for every call, read-only, so a card carries the session's
name and a seat's `session:` finds it; a subagent's calls go by the topmost session's
title. `doctor` finds a seat's session the same way and then checks the gate in its
directory. A `first_input` title is never taken: it is a prompt, not a name, and not
for a card. The database is the application's and its schema is no contract, so
anything that goes wrong reading it means no name — and a seat without one is found by
its project, as it was before.

## What Halyard writes

`halyard wire` merges this into `.zcode/config.json`, backing it up first:

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [{
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__.*",
        "hooks": [{"type": "process", "command": "/usr/bin/env",
                   "args": ["HALYARD_RUNTIME=zcode", "/…/bridge/hook.sh"], "timeoutMs": 600000}]
      }],
      "Stop": [{
        "hooks": [{"type": "process", "command": "/usr/bin/env",
                   "args": ["HALYARD_RUNTIME=zcode", "/…/bridge/relay.py"], "timeoutMs": 15000}]
      }]
    }
  }
}
```

`HALYARD_RUNTIME=zcode` is there because nothing in a call says which runtime made it:
the temporary transcript reads as Claude Code's, and the camelCase copies read as
Antigravity's. Then `wire` asks ZCode whether it trusts the result, and prints the
`grant` command for Halyard's hooks when it does not.

## Delivery (measured, 2026-09-19 and 2026-09-20, engine 0.16.5)

ZCode listens on no port and has no send command, but the engine it ships runs
as `app-server --stdio` and speaks the protocol the application drives it with.
Whoever starts it is the *host*, and the host answers three questions — which
is how Halyard both delivers and keeps its gate. See `halyard.agents.zcode`.

| The engine asks | Halyard answers |
|---|---|
| `session/requestRuntimePreferences` | the four preferences the desktop sends; a session materialises for nobody without them |
| `interaction/requestProviderRuntimeHeaders` | `{"headersApplied": true, "requestAuth": {"apiKey": …}}` — the `ZCODE_TOKEN` from `halyard.yaml`, which the anthropic adapter sends as `x-api-key`. `requestAuth.headers` is refused |
| `interaction/requestPermission` | the gate: `{"decision": "allow"\|"deny", "reason"}` for every side-effect tool |

Sending is `provider/updateAccountConfig` → `session/resume {sessionId,
workspace}` → `session/setMode` → `session/subscribe` → `session/send
{content, modelSelection {providerId, modelId, options {reasoningLevel}}}`, and
each of those is there for a reason measured the hard way:

- **The account snapshot or no models.** Every provider in the application's own
  `~/.zcode/v2/runtime/provider/**/zcode-builtin.json`, each entitled, one
  marked `current`, under `basedOnZCodeBuiltinRevision` of
  `zcode-builtin:<revision>:<sha256 of the catalog's resolved path>` — the hash
  is of the path string, not of the file. A wrong one is answered "received"
  and then materialises nothing at all. `access` must say `zhipu-account`: the
  schema refuses `zhipu-coding-plan-api-key`, and a plan key travels in the
  auth answer instead, which the provider accepts.
- **`resume` wants the workspace**, which `session/list` carries beside each
  session's `title` and `titleSource`.
- **The mode has to be set after opening, every time.** `build` is "Ask before
  changes", `edit` is "Edit automatically", and a session whose stored mode is
  already `build` still runs a `Write` unasked until `session/setMode` is
  called again on the resumed session.
- **Reasoning is mandatory** in the model selection.

What the gate does, measured live on fresh sessions: `allow` runs the tool;
`deny` stops it and the reason reaches the model in its own words ("denied by
the operator's permission gate"); an unanswered request is repeated every few
seconds and the turn never ends — so an expired card must be answered `deny`,
and repeats of one `requestId` are one question.

Headless (`--prompt`) is not a way round any of this: workspace hooks are
`feature_disabled` there, user-scope hooks fire but decide nothing, `--mode
build` refuses every side-effect tool with "No permission client configured",
and no flag or environment variable attaches one.

## Still to measure

- The answer a `PermissionRequest` hook accepts. Not needed while `PreToolUse` gates.
- Whether a hook that times out or fails blocks the call or lets it through.
- Whether a trusted workspace `PreToolUse` hook decides before the permission
  client in `app-server` mode, or both fire — which decides whether a tool can
  raise two cards.
- Whether the desktop shows a turn Halyard ran on a session it has open.
- What MCP tools are called.
