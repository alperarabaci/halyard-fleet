# ZCode hooks — measured

**Version:** ZCode 3.11.2 (`/Applications/ZCode.app`), macOS · **Measured:** 2026-09-15 ·
**Calls captured:** 15, from a probe workspace whose hooks wrote down every one ·
**Status:** the gate (`PreToolUse`) and the reply relay (`Stop`) are wired, and a
session's title is read from ZCode's own database; putting a message into a session is
not.

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

## Still to measure

- The answer a `PermissionRequest` hook accepts. Not needed while `PreToolUse` gates.
- Whether a hook that times out or fails blocks the call or lets it through.
- Any way to put a message into a session from outside the application.
- What MCP tools are called.
