This is the driver's report on one part of the plan for the task in `TASK.md`.
Halyard ran `test-fast` before handing it on, and its last line, above the
report, says how each part of it went: the boundary guard, the contract, the
tests. Weigh the report against that and against the change itself
(`git diff`). Tests that belong to a part still to come may fail; say so, rather
than sending the work back for them.

- This part is done and another is left: write that part for the driver, and
  decide `next`.
- Every part is done: decide `forward`. The work goes on to close.
- This part is not done: say what is missing, written for the driver, and
  decide `back`.

The last line of your answer, alone and with nothing after it:
`DECISION: next`, `DECISION: forward`, `DECISION: back` or `DECISION: wait`.
For `back` or `wait`, the line above it says why.
