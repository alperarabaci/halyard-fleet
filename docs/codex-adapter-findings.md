# Codex adapter measurements

Measured on 2026-07-22 with Codex CLI `0.145.0-alpha.27` from the Codex desktop
application on macOS.

## Bottom line

**The gate is not possible under Halyard's absolute fail-closed contract in
this Codex version.** Codex does have a blocking `PreToolUse` hook, and a valid
deny response or the special `exit 2` response stops a shell command before it
runs. However, every tested hook infrastructure failure ran the command: a
generic non-zero exit, malformed output, timeout, and a command whose
interpreter did not exist. A shell wrapper can translate failures inside the
bridge into `exit 2`, but it cannot protect the path where Codex cannot start
the wrapper itself. That remaining fail-open path violates the stated
constraint.

The rest of the runtime is a good fit: `codex exec resume` writes into the same
session, a UUID and a human-assigned thread name both survive new CLI
processes, and the tested pair of concurrent resumes was preserved without a
fork or lost turn.

All tests used disposable files under
`/private/tmp/halyard-codex-measurements-20260722`. Test sessions were persisted
by Codex under the normal Codex home so that persistence and resume behavior
could be measured.

## 1. Blocking pre-execution hook — measured

Codex has the required hook shape. This command configured a turn-scoped
`PreToolUse` hook inline, asked Codex to run one `touch`, and bypassed the hook
trust UI only for the measurement:

```sh
codex exec \
  -C /private/tmp/halyard-codex-measurements-20260722 \
  --ignore-user-config \
  --dangerously-bypass-hook-trust \
  -s danger-full-access \
  -c 'approval_policy="never"' \
  -c 'hooks.PreToolUse=[{ matcher="^Bash$", hooks=[{ type="command", command="/usr/bin/python3 /private/tmp/halyard-codex-measurements-20260722/hook.py", timeout=1 }] }]' \
  --json \
  'Run exactly this shell command once: /usr/bin/touch marker-deny.'
```

For that run, `case.txt` contained `deny`, and the hook returned:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "measurement-deny"
  }
}
```

Codex printed:

```text
Command blocked by PreToolUse hook: measurement-deny.
Command: /usr/bin/touch marker-deny
```

`marker-deny` was absent. A later run of the same hook from the parent project
layer captured this stdin shape:

```json
{
  "session_id": "019f8b8d-02ba-7df1-9be9-b99c4e5668a5",
  "transcript_path": "/Users/jammer/.codex/sessions/2026/07/22/rollout-2026-07-22T23-38-22-019f8b8d-02ba-7df1-9be9-b99c4e5668a5.jsonl",
  "cwd": "/private/tmp/halyard-codex-measurements-20260722/nested/child",
  "hook_event_name": "PreToolUse",
  "model": "gpt-5.6-sol",
  "permission_mode": "bypassPermissions",
  "turn_id": "019f8b8d-04b5-7610-a3d4-7dc6b87226d5",
  "tool_name": "Bash",
  "tool_input": {"command": "/usr/bin/touch parent-hook-marker"},
  "tool_use_id": "exec-67a61c11-e0d0-48bf-b631-1b0c3a777c6c"
}
```

The hook blocks before shell execution. It also has the fields Halyard needs,
although Codex calls the tool `Bash` and places the command under
`tool_input.command`.

Codex also exposes `PermissionRequest`, but it is not a substitute for this
gate: `PreToolUse` is the measured pre-execution point for shell calls.

## 2. Hook failures — measured

Each case ran in a fresh non-interactive turn. The hook timeout was one second;
the timeout hook slept for three seconds. The marker is the authority: present
means the command ran.

| Hook behavior | Hook result | Marker | Command |
|---|---:|---:|---:|
| Valid JSON `permissionDecision: deny` | exit 0 | absent | **stopped** |
| `exit 7`, no output | exit 7 | present | **ran** |
| exit 0, empty stdout | exit 0 | present | **ran** |
| exit 0, stdout `this is not json` | exit 0 | present | **ran** |
| sleep 3 seconds with timeout 1 | timed out | present | **ran** |
| `/definitely/not/a/real/interpreter ...` | never started | present | **ran** |
| stderr `measurement-exit-2-deny`, then `exit 2` | exit 2 | absent | **stopped** |

The five requested failure cases printed these final checks:

```text
nonzero marker=present
empty marker=present
malformed marker=present
timeout marker=present
no-interpreter marker=present
```

The special exit-code test printed:

```text
Command blocked by PreToolUse hook: measurement-exit-2-deny.
Command: /usr/bin/touch marker-exit2
```

The practical wrapper rule is therefore the same kind of rule Halyard needed
for Claude Code: emit a valid deny or write a reason to stderr and exit 2 on
every inner error. This protects Python failures, HTTP failures, invalid bridge
responses, and bridge-controlled timeouts. It does **not** protect a missing,
untrusted, disabled, or unstartable wrapper, because Codex itself fails open on
that path.

`defer` does not need a new Codex wire value. A successful hook with empty
stdout behaved as no opinion and let Codex continue. In a real gate that is the
translation for Halyard's `defer` response.

## 3. Non-interactive resume into the same session — measured

The initial process created this session:

```text
thread_id=019f8b82-8256-7341-bdc8-e236a8660351
reply=BASELINE-READY
```

A second process, launched from the session cwd, ran:

```sh
codex exec resume \
  -c 'approval_policy="never"' \
  --json \
  019f8b82-8256-7341-bdc8-e236a8660351 \
  'Remember this exact nonce for the next turn: COBALT-LANTERN-48271. Reply with exactly STORED.'
```

It printed the same thread id and `STORED`. A third process ran:

```sh
codex exec resume \
  -c 'approval_policy="never"' \
  --json \
  019f8b82-8256-7341-bdc8-e236a8660351 \
  'What exact nonce did the immediately preceding user message ask you to remember? Reply with only the nonce.'
```

It printed:

```text
thread_id=019f8b82-8256-7341-bdc8-e236a8660351
COBALT-LANTERN-48271
```

There was one transcript file, one `session_meta` record, and three
`turn_context` records. All three processes therefore appended to one session;
this was not a new conversation that happened to know the nonce.

The resume subcommand is sensitive to argument order: resume options must come
before the session id, and it has no `-C` option. The subprocess cwd must be set
by the caller, which already matches `AgentRunner.send(..., cwd)`.

## 4. Two overlapping writes — measured once

Two processes started concurrently against
`019f8b88-167b-7761-9fef-130bae42b829`:

```sh
codex exec resume --ignore-user-config -c 'model_reasoning_effort="low"' \
  --json 019f8b88-167b-7761-9fef-130bae42b829 \
  'Remember overlap token ALPHA-62913 ...' &

codex exec resume --ignore-user-config -c 'model_reasoning_effort="low"' \
  --json 019f8b88-167b-7761-9fef-130bae42b829 \
  'Remember overlap token BETA-77402 ...' &

wait
```

The result was:

```text
alpha_exit=0 beta_exit=0
alpha thread_id=019f8b88-167b-7761-9fef-130bae42b829 reply=ALPHA-STORED
beta  thread_id=019f8b88-167b-7761-9fef-130bae42b829 reply=BETA-STORED
transcript JSONL parse=ok
```

Both nonces were present in the one transcript. A subsequent resume answered:

```text
ALPHA-62913
BETA-77402
```

The Claude Code silent-fork loss did not reproduce. This is one two-writer
trial, not a proof that every overlap is safe. Keeping a per-session lock is
still cheap defensive behavior, but the measurement does not show that Codex
requires it.

## 5. Stable session identity — measured for process restarts

The UUID remained unchanged across every new `codex exec resume` process. Codex
stored the transcript here:

```text
~/.codex/sessions/2026/07/22/
  rollout-2026-07-22T23-32-59-019f8b88-167b-7761-9fef-130bae42b829.jsonl
```

It also indexed sessions in both:

```text
~/.codex/session_index.jsonl
~/.codex/state_5.sqlite  # threads table
```

The app-server method `thread/name/set` was then called with:

```json
{
  "threadId": "019f8b88-167b-7761-9fef-130bae42b829",
  "name": "halyard-overlap-measurement"
}
```

The server returned `{}` as success. The index acquired:

```json
{
  "id": "019f8b88-167b-7761-9fef-130bae42b829",
  "thread_name": "halyard-overlap-measurement"
}
```

The SQLite `threads` row contained the same UUID, title, cwd, model, reasoning
effort, rollout path, and CLI version. Finally:

```sh
codex exec resume --json halyard-overlap-measurement \
  'Reply with exactly NAME-RESUME-OK.'
```

printed:

```text
thread_id=019f8b88-167b-7761-9fef-130bae42b829
NAME-RESUME-OK
```

So either the UUID or a human-assigned thread name can address a persisted
session. A machine reboot and cross-version migration were not measured.

## 6. Per-turn choices and defaults — measured

`codex debug models --bundled` printed this catalog:

| Model | Catalog default effort | Supported effort values |
|---|---|---|
| `gpt-5.6-sol` | `low` | `low medium high xhigh max ultra` |
| `gpt-5.6-terra` | `medium` | `low medium high xhigh max ultra` |
| `gpt-5.6-luna` | `medium` | `low medium high xhigh max` |
| `gpt-5.5` | `medium` | `low medium high xhigh` |
| `gpt-5.4` | `medium` | `low medium high xhigh` |
| `gpt-5.4-mini` | `medium` | `low medium high xhigh` |
| `gpt-5.2` | `medium` | `low medium high xhigh` |
| `codex-auto-review` | `medium` | `low medium high xhigh` |

The bundled catalog is a capability list, not proof of account entitlement for
every row. `gpt-5.6-sol` and `gpt-5.6-terra` were both actually run.

Model and effort can change per resumed turn:

```sh
codex exec resume --ignore-user-config \
  -m gpt-5.6-terra \
  -c 'model_reasoning_effort="high"' \
  --json halyard-overlap-measurement \
  'Reply exactly TERRA-HIGH-OK.'
```

The transcript recorded `{model: gpt-5.6-terra, effort: high}`. Codex warned
that the model differed from the previous turn but completed successfully.

There are two useful meanings of “nothing specified”:

- With the real user config loaded and no model or effort CLI argument, three
  turns recorded `gpt-5.6-sol` and `medium`. Those are the current effective
  defaults from `~/.codex/config.toml`.
- With `--ignore-user-config` and no model or effort override, the turn selected
  `gpt-5.6-sol`; its transcript effort was null and the bundled catalog reports
  that model's default reasoning level as `low`. This is the measured factory
  model plus the CLI's measured catalog default.

Other per-turn app-server settings exist, but they are outside Halyard's
current `/model` and `/effort` contract and were not exercised.

## 7. Project settings and other writers — measured

The project equivalent is `.codex/config.toml`, and hooks may instead live in a
sibling `.codex/hooks.json`. The existing Halyard worktree already has a
`.codex/hooks.json`, so a wire command must merge rather than replace it.

Parent application was measured, not assumed. The disposable Git root had:

```text
/private/tmp/halyard-codex-measurements-20260722/.codex/hooks.json
```

Codex ran with cwd:

```text
/private/tmp/halyard-codex-measurements-20260722/nested/child
```

The root hook received that child cwd and blocked
`/usr/bin/touch parent-hook-marker`; the marker was absent. Therefore a trusted
project root's hook settings apply to descendant working directories.

The real `~/.codex/config.toml` contained all of these unrelated concerns during
the measurement:

- model and reasoning defaults;
- the legacy `notify` command;
- marketplace metadata and enabled plugins;
- per-project trust records;
- desktop UI preferences;
- feature flags;
- MCP server definitions and their environment;
- shell environment policy.

The file changed during the test: a new
`[projects."/private/tmp/halyard-codex-measurements-20260722"]` trust record
appeared after Codex was run there. Because Codex was the only application used
for that project, this is strong evidence that Codex itself writes trust state
to the global config. The exact internal write site was not instrumented.

Accumulated shell permission rules are separate in this installation:

```text
~/.codex/rules/default.rules
```

That file contained the persisted `prefix_rule(...)` approvals. Therefore
merging a project `.codex/hooks.json` does not risk erasing the user's shell
permission rules, but overwriting either global `config.toml` or an existing
project `.codex` file would still destroy unrelated Codex state.

## Smallest `AgentRunner` change

Session delivery itself needs no protocol change. `send(session_id, text, cwd)`
maps directly to a subprocess whose cwd is `cwd` and whose arguments are:

```text
codex exec resume [model/effort options] SESSION_ID TEXT
```

The UUID or thread name can occupy `SESSION_ID`, `send` can keep its
must-not-raise behavior, and the existing `busy` lock can remain even though
the one overlap test succeeded.

One small protocol correction is needed for truthful options: change
`options()` to `options(session_id: str | None = None)`. Codex effort choices
depend on the selected model (`ultra` works for Sol/Terra but is absent for
Luna; `max` is absent from older models). The channel should validate effort
against `runner.options(session_id)` instead of importing Claude Code's
`EFFORT_LEVELS`. Claude Code can ignore the optional argument and return its
current fixed sets. No second runner protocol is needed.

## First end-to-end proof

Do not start with the adapter. Start with an executable conformance test around
the gate: launch a local fake approval endpoint, install one direct shell
wrapper as `PreToolUse`, make it block while the endpoint waits, and assert with
sentinel files that `allow`, `deny`, `defer`, HTTP failure, invalid JSON,
five-minute timeout, bridge crash, wrapper `exit 2`, and an unstartable wrapper
all have the required outcome. The last case will currently fail by running the
sentinel, which is the upstream/runtime blocker to resolve before Halyard can
claim fail-closed support. Once that test passes on a Codex release, add a
single `CodexRunner` proof that names a test thread, calls `codex exec resume`
from its stored cwd, verifies the nonce in the same transcript, and observes a
`Stop` hook relay. Only then is there enough measured behavior to write the
adapter.
