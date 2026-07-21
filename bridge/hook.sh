#!/bin/sh
#
# The wrapper Claude Code actually calls. Point settings.json here, not at the
# Python file.
#
# hook_bridge.py handles its own failures and prints a denial for every one of
# them. This exists for the failures it cannot handle, because they happen
# before it runs: a missing interpreter, a syntax error, a bad shebang, a file
# that is not where settings.json says it is. All of those exit non-zero with
# nothing useful on stdout, and `docs/hook-payload-notes.md` records what Claude
# Code does with that — it treats it as no opinion and runs the command.
#
# So fail-closed cannot live inside the thing that might not start. It lives
# here, in nine lines of POSIX shell with no dependencies of its own: unless a
# real decision came back, print a denial.
#
set -u

DIR=$(dirname "$0")
DENIAL='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Denied by Halyard: the hook bridge could not run. Failing closed."}}'

PYTHON=${HALYARD_PYTHON:-$(command -v python3 || command -v python || true)}
if [ -z "$PYTHON" ]; then
	printf '%s\n' "$DENIAL"
	exit 0
fi

output=$("$PYTHON" "$DIR/hook_bridge.py")
status=$?

# 64 means the bridge deliberately has no opinion — Halyard is paused, and the
# question belongs to Claude Code's own prompt. Say nothing, which is how a
# hook expresses that.
#
# It needs its own code because silence alone cannot carry the meaning: empty
# output is also what a script that died produces, and treating that as "no
# opinion" would turn a crash into consent. So silence still denies, and only
# this code passes through quietly.
if [ "$status" -eq 64 ] && [ -z "$output" ]; then
	exit 0
fi

# A decision, and a clean exit. Anything else is not something to act on.
case "$output" in
*'"permissionDecision"'*)
	if [ "$status" -eq 0 ]; then
		printf '%s\n' "$output"
		exit 0
	fi
	;;
esac

printf '%s\n' "$DENIAL"
exit 0
