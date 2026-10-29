# Commands

What you press on your phone, and what you run on the machine.

## On your phone

| In Telegram | |
|---|---|
| *(type anything)*, `/chat` | send it into that group's session |
| `/prompts` | your prompts — `/md` and any others — as buttons, each sent into the session when pressed |
| `/md` | *(configurable)* have the agent write its answer to a file and pass the path |
| `/stop` | end what is running here: a project command (a transition's or `/command`'s), and the turn in this chat's agent session with the cards it left open — for Claude Code and Codex, a turn sent from Halyard |
| `/options` | every model and effort level the runtime accepts |
| `/model`, `/effort` | what answers, and how hard it thinks |
| `/command` | run one of the project's own commands, and hear how it went |
| `/commit` | commit this branch's work, with a message to approve — and push; with `commit_options:`, plainly or after one of the project's own checks |
| `/review_and_commit` | *(typed; not on the menu)* the project's checks: its one commit option, or its options without the plain Commit |
| `/label` | put a label on the task this branch is for |
| `/forward` | hand this chat's last reply to another agent |
| `/to` | *(typed; not on the menu)* hand a message to another agent by name |
| `/inspect` | pick one of this project's own inspections and run it over the chat's last reply; the answer has a button per agent to hand it on |
| `/transition` | take the chat's last reply to its next stage, the way this project defines it — its own prompt in front, its commands and inspections run first |
| `/workflow` | take this project's transitions in the order it wrote them down, each reply's last line deciding the next step |
| `/status` | what each agent is, what is running, and how full its limits are — and this group's id, and whose it is |
| `/doctor` | the same check as `halyard doctor`, read from a phone |
| `/open` | open an app on the machine — `claude`, `codex`, `gemini` — with no card |
| `/pause`, `/resume` | step out of the way, and come back |
| `/actions`, `/agent` | status, pause, resume and stop — or options, model and effort — as buttons |
| `/help` | every command, and your prompts |

`TELEGRAM_MENU: simple` shows a short menu — `/actions`, `/stop`, `/agent`, `/prompts`,
`/commit`, `/open`, `/doctor` and `/help` — with the rest a card away; the full one,
every command and prompt, is the default. Every command answers when typed either way,
and `/help` lists them all. `halyard init` and the examples start on the short one.

`/md` is one of the `prompts:` in `halyard.yaml` — a sentence you say often
enough to want a name for. Each key there becomes a command. It exists because
a phone is bad at moving text: a long answer arrives split into three messages,
and handing it on means copying each piece while the agent receiving them
starts working on a third of the instruction. Better not to move it at all.

When Claude Code asks a **multiple-choice question** (its `AskUserQuestion`
tool), the options arrive on your phone as buttons — tap one, or reply with your
own answer. The choice goes straight back into the session, and the picker at
the desk never appears. Nobody answers in time, or Halyard is paused, and it
falls back to that picker rather than deciding for you. Claude Code only for now.

## On the machine

| On the machine | |
|---|---|
| `halyard init` | guided setup: `halyard.yaml`, wiring, and a check |
| `halyard samples add` | put a [sample](../samples/)'s projects into `halyard.yaml`, keeping a backup |
| `halyard doctor` | what is wired, where, and what is broken |
| `halyard verify` | prove the gate stops things, by running into it |
| `halyard wire` / `unwire` | put the gate on a project, or take it off |
| `halyard sessions` | session names this machine can see |
| `halyard rules` | the commands each project trusts to run without asking; add, export, import |
| `halyard upkeep` | jobs Halyard does about itself — `runs-advice`: what a project could trust, from the cards it still asks; `recent` and `show` read past runs back |
| `halyard runs` | what workflow runs did: the latest, or one work's steps and decisions |
| `halyard inspect` | the inspection runs Halyard kept: `recent`, `repeat` one on another model, `compare` them side by side |
| `halyard usage` | what the turns Halyard started itself used, by model and purpose |
| `halyard export` | everything Halyard keeps — `halyard.yaml`, the database, the audit log, prompts, kept inspections, logs, and the files your projects' configuration points at (transition prompts, inspections) — in one dated zip under `export/`, readable by you only; it holds your bot token |
| `halyard service install` | run it as a launchd service (macOS) that updates itself first |

`halyard service install` sets up a launchd agent that comes back after a crash
and after a reboot. Every time it starts it runs `git pull --ff-only`, then
`uv sync --inexact`, then serves — so the machine you leave running stays
current without you logging in to update it, and a checkout you also develop in
keeps the tools `uv sync --extra dev` installed. The pull is fail-open: it never
rewinds or touches local changes, and a pull it cannot fast-forward is skipped
so the last known-good code still serves. It runs the code it pulls, so point
the branch at a remote you control; `install` prints which one. `uninstall` and
`status` do what they say. macOS only — on Linux, run `halyard serve` under a
systemd unit.

---

[← Back to the README](../README.md)
