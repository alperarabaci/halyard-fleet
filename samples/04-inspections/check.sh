#!/bin/sh
# The page's own check: the list, the checkbox that hides what is packed, and
# the count. It only looks for the ids; whether the page does what `TASK.md`
# asks is what the `claims` inspection reads the change for.

fail=0
for id in items hide-packed left; do
  if grep -q "id=\"$id\"" index.html; then
    echo "ok   #$id"
  else
    echo "MISSING #$id"
    fail=1
  fi
done
exit $fail
