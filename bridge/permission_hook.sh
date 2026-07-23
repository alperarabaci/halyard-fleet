#!/bin/sh
#
# Codex's PermissionRequest event uses a different output contract from
# PreToolUse. This wrapper is intentionally separate so its last-resort denial
# cannot accidentally be interpreted as "no opinion" and fall back to a
# desktop prompt.
#
set -u

DIR=$(dirname "$0")
DENIAL='{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":"Denied by Halyard: the hook bridge could not run. Failing closed."}}}'

PYTHON=${HALYARD_PYTHON:-$(command -v python3 || command -v python || true)}
if [ -z "$PYTHON" ]; then
	printf '%s\n' "$DENIAL"
	exit 0
fi

output=$("$PYTHON" "$DIR/hook_bridge.py")
status=$?

# Halyard is paused. A deliberate silence returns this question to Codex,
# which is exactly where it lived before the hook was installed.
if [ "$status" -eq 64 ] && [ -z "$output" ]; then
	exit 0
fi

case "$output" in
*'"PermissionRequest"'*'"behavior"'*)
	if [ "$status" -eq 0 ]; then
		printf '%s\n' "$output"
		exit 0
	fi
	;;
esac

printf '%s\n' "$DENIAL"
exit 0
