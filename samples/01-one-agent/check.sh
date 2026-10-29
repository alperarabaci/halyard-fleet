#!/bin/sh
# The page's own check: every button the page should have is there.
# Run by the agent once it has changed the page, so its run is a card on your phone.

fail=0
for id in count add reset; do
  if grep -q "id=\"$id\"" index.html; then
    echo "ok   #$id"
  else
    echo "MISSING #$id"
    fail=1
  fi
done
exit $fail
