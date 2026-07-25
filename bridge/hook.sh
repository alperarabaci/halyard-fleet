#!/bin/sh
#
# The wrapper the runtimes actually call. Point their settings at this, not at
# the Python file.
#
# hook_bridge.py handles its own failures and prints a denial for every one of
# them. This exists for the failures it cannot handle, because they happen
# before it runs: a missing interpreter, a syntax error, a bad shebang, a file
# that is not where settings.json says it is. All of those exit non-zero with
# nothing useful on stdout, and `docs/hook-payload-notes.md` records what that
# means — it is treated as no opinion, and the command runs.
#
# So fail-closed cannot live inside the thing that might not start. It lives
# here, in POSIX shell with no dependencies of its own: unless a real decision
# came back, print a denial.
#
# **The denial has to be in the right dialect, and there is no merging them.**
# Claude Code and Codex read `hookSpecificOutput`; Antigravity reads a flat
# `decision`. Measured against a live Antigravity conversation, with a hook
# proven by a witness file to have fired, on a command nobody had approved
# before:
#
#     hookSpecificOutput alone       the command ran
#     both keys in one object        the command ran
#     flat `decision` alone          blocked, with no prompt at all
#
# So the extra key does not get ignored — it stops the whole answer being
# understood, and an answer that is not understood is an approval. One payload
# for every runtime serves none of them.
#
# This script cannot parse JSON, so it looks for a string only Antigravity
# sends. `toolCall` is the tool object at the top of its PreToolUse payload;
# nothing in Claude Code's or Codex's payloads contains it. A false negative
# denies in the wrong dialect, which is the harmless direction; a false
# positive would need another runtime to invent the same field name.
#
set -u

DIR=$(dirname "$0")
REASON="Denied by Halyard: the hook bridge could not run. Failing closed."
CLAUDE_DENIAL="{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"$REASON\"}}"
ANTIGRAVITY_DENIAL="{\"decision\":\"deny\",\"reason\":\"$REASON\"}"

# stdin is read once, here, because it has to be examined *and* handed on. A
# hook only gets it once.
payload=$(cat)

case "$payload" in
*'"toolCall"'*) DENIAL="$ANTIGRAVITY_DENIAL" ;;
*) DENIAL="$CLAUDE_DENIAL" ;;
esac

PYTHON=${HALYARD_PYTHON:-$(command -v python3 || command -v python || true)}
if [ -z "$PYTHON" ]; then
	printf '%s\n' "$DENIAL"
	exit 0
fi

output=$(printf '%s' "$payload" | "$PYTHON" "$DIR/hook_bridge.py")
status=$?

# 64 means the bridge deliberately has no opinion — Halyard is paused, and the
# question belongs to the runtime's own prompt. Say nothing, which is how a
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
# Either dialect counts: the bridge knows which runtime asked and this does not
# need to second-guess it.
case "$output" in
*'"permissionDecision"'* | *'"decision"'*)
	if [ "$status" -eq 0 ]; then
		printf '%s\n' "$output"
		exit 0
	fi
	;;
esac

printf '%s\n' "$DENIAL"
exit 0
