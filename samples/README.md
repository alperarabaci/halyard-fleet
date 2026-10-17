# Samples

Each sample is a tiny project you can run under Halyard as it is: a page or a script,
the `halyard.yaml` it needs, and the prompts it uses. They start with one agent and
build up, one idea at a time.

Every sample is copied out of this checkout before it is gated — `halyard wire` gates
the repository a folder is in — and each one says how.

| | Sample | What it shows |
|---|---|---|
| 01 | [One agent](01-one-agent/) | One agent on your phone: its commands as cards, its replies, a commit |
| 02 | [Two agents, two runtimes](02-two-agents/) | Claude Code and Codex on two small products, each in its own chat; a project command |
| 03 | Handing work over | A navigator and a driver; `/to`, `/forward`, and a transition with a short prompt |
| 04 | Inspections | A transition that has the work inspected before it is handed on |
| 05 | The whole mechanism | Commands, trusted runs, task labels, `/command` and `/label` |
| 06 | A simple workflow | Two steps, each reply's last line deciding what comes next |
| 07 | Three agents | A navigator, a driver and a reviewer, in a workflow with phases |

Samples without a link are planned, and come one at a time.
