#!/bin/sh
# Runs a Makefile target's parts one by one, and answers the way Halyard reads a
# command: an exit code that means one thing — 0 passed, 1 failed, 2 did not run
# — and one last line with every part in it. `make` alone can say neither: it
# exits 2 for any failure, and its last line is its own.
#
#   scripts/validate.sh test-fast
#
# The parts are what the Makefile lists after the target's colon; a target with
# none is run as it is. The first part that does not pass stops the rest.

target="$1"
case "$target" in
  "" | *[!A-Za-z0-9_-]*)
    echo "validate: exit 2 — say which target, in letters, digits, - and _"
    exit 2
    ;;
esac
if ! grep -q "^$target:" Makefile 2>/dev/null; then
  echo "$target: exit 2 — the Makefile has no target $target"
  exit 2
fi
parts=$(sed -n "s/^$target:\([^#]*\).*/\1/p" Makefile | head -n 1)
[ -n "$(echo $parts)" ] || parts="$target"

logs=$(mktemp -d) || exit 2
trap 'rm -rf "$logs"' EXIT
make_says='^make\(\[[0-9]*\]\)\{0,1\}: \*\*\*'
summary=""
overall=0
stopped=""
for part in $parts; do
  if [ -n "$stopped" ]; then
    summary="$summary; $part: not run (stopped after $stopped)"
    continue
  fi
  make -s "$part" > "$logs/$part" 2>&1
  status=$?
  if [ "$status" -ne 0 ]; then
    # make exits 2 whatever its recipe did; the recipe's own code is in its
    # last words, `make: *** [Makefile:8: boundaries] Error 1`.
    recipe=$(sed -n "s/$make_says.*Error \([0-9]*\).*/\2/p" "$logs/$part" | head -n 1)
    [ -n "$recipe" ] && status="$recipe"
    [ "$status" -eq 1 ] || status=2
  fi
  grep -v "$make_says" "$logs/$part"
  last=$(grep -v "$make_says" "$logs/$part" | grep -v '^[[:space:]]*$' | tail -n 1)
  summary="$summary; $part: ${last:-(nothing printed)}, exit $status"
  if [ "$status" -ne 0 ]; then
    overall="$status"
    stopped="$part"
  fi
done
echo "$target: exit $overall — ${summary#; }"
exit "$overall"
