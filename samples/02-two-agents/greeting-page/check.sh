#!/bin/sh
# The page's own check: the greeting is there, and so is the field it answers to.
# The agent runs it when it is done, and `/command check` runs it from your phone.

fail=0
for id in greeting name; do
  if grep -q "id=\"$id\"" index.html; then
    echo "ok   #$id"
  else
    echo "MISSING #$id"
    fail=1
  fi
done
exit $fail
