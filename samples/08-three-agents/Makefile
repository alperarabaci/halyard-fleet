# The project's checks. `test-fast` is all of them, the fastest to fail first:
# the boundaries, then what the cart needs from the catalog, then each
# context's own tests. scripts/validate.sh runs them one by one for Halyard.
.PHONY: test-fast boundaries contract unit
test-fast: boundaries contract unit

boundaries:
	python3 scripts/guard.py

contract:
	python3 -m unittest discover -q -s contracts -t .

unit:
	python3 -m unittest discover -q -s tests -t .
