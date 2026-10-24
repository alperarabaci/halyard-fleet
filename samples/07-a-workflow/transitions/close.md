This is the driver's report on the task in `TASK.md`, to close the work. Above
it are what the tests printed when Halyard ran them, `test-fast`, and the
answers of the `test-reliability` and `over-engineering` inspections. Weigh them
against the change itself (`git diff`): an inspection reads the change but can
be wrong, and what it could not measure is still open. If the task is done, say
so in a line and decide `done`. If not, say what is missing — written for the
driver — and decide `back`. The decision goes alone on the last line, with
nothing after it; for `back`, the line above it says why.
