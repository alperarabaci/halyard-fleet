This is the driver's report on the task in `TASK.md`, to close the work. Above
it are the last line of `test-fast`, which Halyard ran — the boundary guard, the
contract, the tests — and the `bounded-context` inspection's answer, from
another model. Weigh them against the change itself (`git diff`): an inspection
can be wrong, and what it could not measure is still open.

- The task is done, and both contexts are whole: say so in a line, and decide
  `done`.
- It is not: say what is missing, written for the driver, and decide `back`.

The last line of your answer, alone and with nothing after it:
`DECISION: done`, `DECISION: back` or `DECISION: wait`. For `back` or `wait`,
the line above it says why. Only `done` ends the run.
