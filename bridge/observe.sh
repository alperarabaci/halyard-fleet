#!/usr/bin/env bash
#
# Optional payload observer. NOT the production hook — point Claude Code at
# bridge/hook.sh for that (see the README).
#
# This exists because the design rests on what a PreToolUse payload actually
# contains, and that is worth being able to re-verify on your own Claude Code
# version rather than trusting docs/hook-payload-notes.md. Wire it up as a
# PreToolUse hook in .claude/settings.local.json, run a few commands, and read
# the log.
#
# It is passive by construction: it appends the raw stdin payload to a log file
# and exits 0 writing nothing to stdout. Empty stdout means "no opinion", so the
# normal permission flow is left completely untouched.
#
set -uo pipefail

LOG_FILE="${HALYARD_OBSERVE_LOG:-/tmp/halyard-hook.log}"

{
	printf '===== %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	cat
	printf '\n'
} >>"$LOG_FILE" 2>/dev/null

exit 0
