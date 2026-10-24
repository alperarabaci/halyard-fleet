The text below is the driver's report on the task in `TASK.md`. Read; change
nothing.

Read the tests the change relies on — `tests/` and `contracts/`, and `git diff`
for any test it added or touched — and say how far they can be trusted to catch
the task done wrong:

- Covered: each part of `TASK.md`, and the test that would fail without it.
- Weak: a test that cannot fail, asserts too little, or depends on order, time
  or chance; and any change under `tests/` or `contracts/`, since `TASK.md` asks
  for changes to `catalog/` and `cart/` only.
- Missing: a part of `TASK.md`, or an edge it names, that no test would catch;
  and anything the cart now uses from the catalog that `contracts/` does not list.

Read the tests; do not run them. A test you could not read is not measured,
never clean. Keep it under fifteen lines.
