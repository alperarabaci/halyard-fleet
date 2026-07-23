# Blameless Postmortem: Codex Telegram Routing Gaps

**Date:** 2026-07-23  
**Status:** Corrective changes implemented locally; activation pending review and rewiring  
**Affected paths:** Codex permission escalation and Telegram-to-Codex message delivery

## Summary

Four independent boundaries were exposed while adding Codex beside Claude
Code:

1. A command approved through the `PreToolUse` hook reached a second Codex
   sandbox-escalation decision. Halyard had no `PermissionRequest` hook, so that
   decision appeared in the desktop application instead of Telegram.
2. A message typed in the Telegram chat for `alpha-engine-xdriver` resolved the
   correct Codex session, then lost its runtime association before delivery.
   The default Claude Code runner received the Codex session ID and reported
   `No conversation found with session ID`.
3. After the runtime handoff was corrected, the same message exposed a second
   delivery guard: the Codex runner could not recover the session's working
   directory from a 283 MB rollout because it inspected only the final 256 KiB.
   The directory was present in the rollout, but outside that fixed window.
4. Agent replies retained only their role on the way to Telegram. A Claude
   driver and a Codex driver therefore became indistinguishable at the final
   channel call, and a Codex reply could land in the default bot chat or the
   first driver chat instead of the group where the turn began.

No command was executed without approval. The escalation remained blocked at
the desktop prompt, and the Telegram message failed visibly and was recorded as
undelivered. Later successful phone turns were verified in the original Claude
and Codex transcripts; the open desktop chat views did not live-refresh them,
but the turns were not lost or written to side conversations.

## Impact

- A user could approve a command in Telegram and still need to answer a second
  permission prompt at the desktop when Codex requested elevated sandbox access.
- Messages sent from the Codex seat's Telegram chat did not reach the Codex
  conversation.
- Successful replies could reach Telegram's default chat instead of the
  runtime-specific group that originated the turn.
- A phone-started turn could be absent from an already-open desktop chat view
  even though it had been appended to the same persisted task.
- The native desktop presentation described the escalation as important, while
  Halyard classified the command itself as low risk. The two labels came from
  different systems and made diagnosis less direct.
- Claude Code message delivery and its existing allowlist were not changed by
  the incident.

## Detection

The incident was detected through two user-visible symptoms:

- The Codex desktop application displayed:

  > Do you want to allow checking whether the E2E process is still running
  > after the tool session unexpectedly detached, so I do not start a
  > competing run?

- Telegram answered:

  > That did not reach the session. Check the control plane's log.

- A later successful reply appeared in the main bot chat rather than the
  configured Codex group.

The control-plane log supplied the boundary failure:

```text
halyard.agents.claude_code.runner: Delivering a message to
<codex-session-id> failed (exit 1):
No conversation found with session ID:
<codex-session-id>
```

## Timeline

- The Codex runtime was added alongside an architectural change that introduced
  runtime-specific runners and configurable seats.
- Codex `PreToolUse` and `Stop` hooks were wired and trusted.
- A process-inspection command was sent through the Codex session.
- Halyard relayed and resolved the `PreToolUse` approval through Telegram.
- The sandbox refused `ps`, and Codex retried with
  `sandbox_permissions="require_escalated"`.
- The retry passed through Halyard's `PreToolUse` gate, but Codex then raised a
  separate `PermissionRequest` event. No Halyard hook was registered for that
  event, so Codex used its desktop approval surface.
- Separately, a Telegram message resolved the configured Codex seat and session.
  Delivery called the channel's default Claude Code runner rather than the
  runner used during resolution, so the message failed.
- After that handoff was corrected, a retry reached the Codex runner and was
  safely refused because the resolver reported no working directory.
- Inspection of the exact 283 MB rollout found the latest context 350 records
  before the end. Both `session_meta` and `turn_context` recorded the expected
  `alpha-engine` directory; neither appeared in the old 256 KiB tail window.
- A successful Codex reply then exposed a separate outbound route loss:
  `MessageRelay` received `agent_id`, `session_name`, and `session_id`, but
  called the channel with only `role`. The Telegram adapter therefore had the
  correct multi-runtime route table and no identity with which to use it.
- The successful Codex phone turn was found in the same 283 MB rollout under
  the original task ID. The Codex desktop thread backend also returned that
  turn, including its user message, progress, file changes, and final answer.
- A Claude phone turn was likewise found in the original named transcript with
  a continuous parent chain. This ruled out a second conversation, a transcript
  fork, and a wrong-runner write for the live-view symptom.
- A comparison task created by `codex-tui` appeared as a new desktop task. That
  proves CLI-created tasks are indexed by the desktop application, but it is a
  different case from live-refreshing an already-open task after an external
  `codex exec resume`.
- The installed Codex hook schemas and the persisted transcript were inspected.
  The two decision stages and the runner handoff were confirmed independently.
- After an explicit `unwire` followed by `wire`, the backup chain showed the
  full Codex hook document, the unwired `{}` document, and the newly wired
  document as three distinct files. The live document contained
  `PermissionRequest`, and Codex subsequently wrote its
  `permission_request:0:0` trust record to `~/.codex/config.toml`. No further
  trust prompt was expected once that record existed.
- Corrective code and regression tests were added locally.

## Root Causes

### Permission escalation

The integration treated a successful `PreToolUse` decision as the complete
Codex permission contract. In Codex, `PreToolUse` and `PermissionRequest` are
separate blocking events with different output schemas. `PreToolUse` gates the
tool call; it does not grant a later native sandbox escalation.

### Message delivery

Session resolution was runtime-aware, but its return value contained only the
session ID, project, and working directory. Delivery therefore used the
channel's legacy default runner. A Codex session ID without its owning runtime
was an incomplete address.

### Working-directory recovery

The Codex resolver assumed a fixed 256 KiB transcript tail would always contain
a recent context record. Long-lived application tasks can append hundreds of
tool and response records after the latest context. The resolver therefore
confused “not present in this byte window” with “not recorded for this session.”
The runner's refusal to resume without a directory was correct and prevented the
task from being continued under another project's hooks.

### Telegram reply routing

Approval cards used the full seat address
`(role, session_name, agent_id, session_id)`. Plain replies crossed an older
channel protocol that carried only `(session_id, text, role)`, and
`MessageRelay` discarded the runtime and session name before that call. Role
was sufficient when only Claude Code existed; it became ambiguous as soon as
both runtimes had a driver seat.

### Desktop live view

Headless resume writes durable history, but durable history and an already-open
desktop view are two different delivery boundaries. In the Codex case, the
rollout and desktop thread backend both contained the complete Halyard turn. In
the Claude case, the original transcript contained the complete turn on the
existing parent chain.

The Claude symptom was initially grouped with the Codex presentation boundary,
but further evidence identified a separate Claude-specific regression in
binary selection and model inheritance. It is documented independently in
[Claude Desktop Live Session Synchronization Regression](2026-07-23-claude-desktop-live-session-sync.md).

## Contributing Conditions

- The runtime addition and the channel restructuring landed close together,
  making it harder for tests to distinguish a Codex adapter defect from a
  shared routing defect.
- Single-runtime tests could not expose a wrong-default-runner call because the
  resolver and default runner were the same object.
- The initial Codex hook proof measured `PreToolUse` as a blocking gate but did
  not exercise a command that subsequently required native sandbox escalation.
- The desktop application's importance label described the native escalation,
  not Halyard's risk classification, but the UI did not make that distinction
  visible.

## What Went Well

- Both paths failed visibly rather than silently.
- The approval path remained fail-closed; no missing hook decision became an
  automatic approval.
- Audit and application logs contained enough session and runner information to
  identify the handoff boundary.
- The existing `AgentRunner` protocol fit Codex. The correction did not require
  a parallel runtime protocol or a rewrite of the Claude Code adapter.
- The existing wire implementation already used merge semantics and timestamped
  backups, providing the correct foundation for adding another Codex hook.

## Corrective Actions

### Implemented locally

- Carry the owning `AgentRunner` together with every resolved session through
  busy checks, delivery, model selection, effort selection, and option display.
- Add a Codex-only `PermissionRequest` hook during `halyard wire`.
- Translate `PermissionRequest` decisions into Codex's measured decision shape.
- Add a separate fail-closed shell wrapper for the permission-event schema.
- Preserve the native escalation justification on the Telegram approval card.
- Add multi-runtime regression tests proving that a Codex seat never calls the
  Claude Code runner.
- Extend the channel contract so replies retain `agent_id` and `session_name`
  through `MessageRelay`, and route Telegram messages with the same full seat
  address used by approval cards.
- Keep long replies and full-command documents in the same runtime-specific
  group or forum topic.
- Read large Codex rollouts backwards by complete JSONL records until the newest
  context is found, without loading the transcript or oversized tool output into
  memory.
- Add a regression rollout whose context sits beyond the former fixed-size tail.
- Add bridge and wiring tests for the Codex-only permission event.
- Make `halyard doctor` say when every Codex hook has a persisted trust record,
  so the absence of another review prompt is explainable.
- Stop using a conversation's creation timestamp as evidence that the current
  desktop process predates a settings change. Resume keeps the conversation
  timestamp, so that comparison produced a permanent false restart warning.
- Report the selected Claude executable in `halyard doctor`, making future
  Desktop/CLI version skew visible instead of implicit.

### Activation pending review

- Run `halyard wire` for each affected project. The command must merge into the
  existing runtime configuration and create a backup before writing.
- Review and trust the newly added Codex hook.
- Restart the Codex task or application so Codex reloads the hook configuration.
- Restart the Halyard control plane so it loads the runtime-routing correction.
- Complete the independent Claude Desktop live-session verification described
  in its postmortem.

## Prevention and Follow-up

- Keep a fixture with Claude Code and Codex seats active at the same time for
  every channel-to-session test.
- Treat a session address as `(runtime, session_id)`, never as a bare session ID.
- Treat a channel seat as `(runtime, role, session)`, never as a role alone.
- Exercise both the ordinary tool gate and native sandbox escalation in Codex
  conformance checks.
- Keep `PermissionRequest` wiring Codex-only so Claude Code's established hook
  and allowlist behavior is unchanged.
- Continue to merge project settings structurally, never replace the document.
  Back up the exact pre-write bytes before every material wire or unwire change.

## Lessons

An adapter boundary is not complete when it can resolve an identifier; it is
complete when the identifier reaches the operation together with the runtime
that gives it meaning. The same is true in the other direction: routing is not
complete until a reply reaches the channel with enough identity to select its
originating seat. Likewise, an approval surface is not one generic event: each
blocking stage that can stop execution must be measured and wired explicitly.

This incident resulted from reasonable assumptions that were not represented in
multi-runtime and escalation tests. The corrective work focuses on making those
assumptions executable and observable rather than attributing the failure to
any individual action.
