#!/usr/bin/env bash
#
# PreToolUse hook used only for payload observation (Phase 1, step 1).
#
# By default it is passive: it appends the raw stdin payload to a log file and
# exits 0 writing nothing to stdout. An empty stdout means "no opinion", so the
# normal permission flow is left completely untouched.
#
# It also carries deny probes, used to answer the two questions that cannot be
# answered by watching alone: which decision output format Claude Code actually
# honors, and what it does to the session when a hook denies a call. A probe
# fires only for Bash calls whose command contains the corresponding marker.
# Restricting probes to Bash is deliberate — otherwise editing this very file
# would embed a marker in a Write/Edit payload and the hook would deny its own
# modification.
#
# This script is throwaway scaffolding. It is replaced by bridge/hook_bridge.py
# once the payload contract is documented in docs/hook-payload-notes.md.
#
set -uo pipefail

LOG_FILE="${HALYARD_OBSERVE_LOG:-/tmp/halyard-hook.log}"

payload="$(cat)"

{
	printf '===== %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	printf '%s\n' "$payload"
} >>"$LOG_FILE" 2>/dev/null

# Probes only apply to Bash calls.
case "$payload" in
*'"tool_name":"Bash"'*) ;;
*) exit 0 ;;
esac

case "$payload" in
*HFPROBE_DENY_W*)
	# Documented form: hookSpecificOutput wrapper with permissionDecision.
	printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Halyard probe: denied via the hookSpecificOutput wrapper form."}}'
	;;
*HFPROBE_DENY_L*)
	# Legacy form: bare decision/reason pair.
	printf '%s\n' '{"decision":"block","reason":"Halyard probe: denied via the legacy decision/block form."}'
	;;
*HFPROBE_ALLOW_W*)
	# Documented allow form: should bypass the permission prompt entirely.
	printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Halyard probe: allowed via the hookSpecificOutput wrapper form."}}'
	;;
*HFPROBE_GARBAGE*)
	# Malformed stdout: does Claude Code fail open or closed?
	printf '%s\n' 'this is not json at all'
	;;
*HFPROBE_EXIT2*)
	# Exit code 2 is documented as a blocking error. This is the bridge's last
	# line of defense if it ever fails before it can print a deny decision.
	printf '%s\n' 'Halyard probe: blocking via exit code 2.' >&2
	exit 2
	;;
*HFPROBE_EXIT1*)
	# Any other non-zero code is documented as non-blocking. Verify that an
	# unhandled crash really does let the command through.
	printf '%s\n' 'Halyard probe: unhandled error via exit code 1.' >&2
	exit 1
	;;
*HFPROBE_TIMEOUT*)
	# Sleep well past the 10s timeout configured for this hook. Determines
	# whether a hook that never answers fails open or closed.
	sleep 25
	printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Halyard probe: too late to matter."}}'
	;;
esac

exit 0
