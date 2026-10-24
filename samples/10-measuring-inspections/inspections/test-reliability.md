The text below is the agent's report on the task in `TASK.md`. Read; change
nothing.

Read the tests — `test_cart.py`, and `git diff` for any test the change added or
touched — and say how far they can be trusted to catch the task done wrong:

- Covered: each part of `TASK.md`, and the test that would fail without it.
- Weak: a test that cannot fail, asserts too little, depends on order, time or
  chance, or was changed to fit the code rather than the task.
- Missing: a part of `TASK.md`, or an edge it names, that no test would catch.

Read them; do not run them. A test you could not read is not measured, never
clean. Keep it under fifteen lines.
