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
| 03 | [Handing work over](03-handing-work-over/) | A navigator and a driver; `/to`, `/forward`, and a transition with a short prompt |
| 04 | [Inspections](04-inspections/) | A transition that has the work inspected before it is handed on |
| 05 | [The whole mechanism](05-the-whole-mechanism/) | Commands, trusted runs, task labels, `/command` and `/label` |
| 06 | [Transitions](06-transitions/) | A navigator and a driver handing work there and back with `/transition`, the tests run on the way |
| 07 | [A workflow](07-a-workflow/) | The same transitions as steps, each reply's last line deciding what comes next |
| 08 | [Three agents](08-three-agents/) | A navigator and a driver on Claude, a reviewer on GPT, and two bounded contexts kept apart by a guard, a contract and inspections, in a workflow with phases |
| 09 | Fewer cards | What the project trusts, proposed from the cards it still asks: `halyard upkeep runs-advice` and `writes:` |
| 10 | [Measuring inspections](10-measuring-inspections/) | Every inspection run kept: the same input on another model, side by side with its tokens, and a prompt's revisions compared; `/doctor`, `/open` and `halyard export` around it |

Samples without a link are planned, and come one at a time.
