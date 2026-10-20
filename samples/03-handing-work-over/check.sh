#!/bin/sh
# The page's own check: the list, the field that filters it, and the count.
# The driver runs it when it is done, and the navigator reads what it printed.

fail=0
for id in books filter shown; do
  if grep -q "id=\"$id\"" index.html; then
    echo "ok   #$id"
  else
    echo "MISSING #$id"
    fail=1
  fi
done
exit $fail
