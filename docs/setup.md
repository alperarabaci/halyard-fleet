# Installing and wiring it up

Getting the control plane running, and putting the gate on a project.

> Wiring a project makes it depend on this process. Read
> [the three rules](../README.md#read-this-before-you-wire-it-into-anything) first.

## Running it

```bash
cp .env.example .env       # then set HALYARD_CHANNEL
uv sync --extra dev
uv run halyard
```

### It runs on your machine, not in a container

There was a Docker image for a while, and removing it is the honest correction to a mistake.

The control plane sends messages into a Claude Code session by running the `claude` CLI, and it
reads session names out of `~/.claude/projects`. A container has neither — no binary, no
credentials, no home directory. So a containerised control plane could relay approvals and output
but could never accept a message back, which is half the product, and having two ways to run it
that quietly differ in what they can do is worse than having one.

`halyard doctor` reports `can_send_messages`, so if this ever regresses it says so rather than
failing at the moment you need it.

### Wiring the hooks

One command, from anywhere inside the project:

```bash
halyard wire .        # or: halyard wire ~/code/my-project
halyard unwire .      # take it back off
```

It merges into `.claude/settings.local.json` rather than replacing it, copies the
file aside first with a timestamp, and finds the repository root on its own — a
session opened in a subdirectory is gated from the top of its repo, so wiring
next to the session would gate nothing while looking like it had.

Unwiring removes only the hooks pointing at *this* install, so it cannot
uninstall a hook it did not install.

The rest of this section is what that command writes, for when you want to check
it or do it by hand:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/halyard-fleet/bridge/hook.sh",
            "timeout": 600
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/halyard-fleet/bridge/relay.py",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

Use an absolute path when the project being gated is not Halyard itself — `$CLAUDE_PROJECT_DIR`
points at whichever repository the session is in. Put it in `.claude/settings.local.json`, which is
gitignored, so a machine-specific path does not break a teammate's checkout.

> **Merge it into that file. Do not replace the file.**
>
> `settings.local.json` is not yours — Claude Code writes to it too. Every time you answer a
> permission prompt with "don't ask again", the rule is appended to a `permissions.allow` list
> that lives in exactly this file. Pasting the block above over the top deletes that list, and
> nothing announces it: approvals still work, the hook still fires, and the only symptom is that
> the session starts asking about commands it stopped asking about months ago.
>
> It is also gitignored, so there is no history to recover it from. Copy the file somewhere first.
>
> ```bash
> cp .claude/settings.local.json .claude/settings.local.json.bak
> ```
>
> The result should have both keys side by side:
>
> ```json
> {
>   "hooks": { "PreToolUse": [ ... ], "Stop": [ ... ] },
>   "permissions": { "allow": [ "Bash(uv run *)", "..." ] }
> }
> ```

**Claude Code snapshots hook configuration at startup.** Editing settings mid-session has no
effect; restart the session. Script contents are read on every call, so those can change freely.

**`PreToolUse` → `hook.sh`** is the approval gate. Point it at the wrapper, not at
`hook_bridge.py`: the wrapper is what denies when the Python process cannot start at all — a
missing interpreter, a bad path, an import error. Those exit non-zero with nothing on stdout, which
Claude Code reads as *no opinion*, and it runs the command.

**`Stop` → `relay.py`** sends the agent's replies to your phone. It is optional; approvals work
without it.

The two have opposite rules, which is why they are separate files. The approval bridge denies on
every error, because something is waiting on its answer. The relay swallows every error and prints
nothing, because a lost chat message is not worth interrupting a session over. Neither should ever
be given the other's behaviour.

### Once the hook is wired, the terminal stops asking

This is the part worth understanding before you wire it up.

A `PreToolUse` hook decides *instead of* Claude Code's own permission prompt, not alongside it. So
from the moment the hook is installed, **the prompt in your terminal no longer appears for the
tools it matches** — the question goes to your phone and the answer comes back from there. Sitting
at the keyboard does not give you a second way to say yes.

The consequence to plan for: if the control plane is not reachable, every matching command is
denied, and there is no terminal fallback to approve it with. That is the fail-closed guarantee
working correctly, and it means the recovery path is fixing the control plane — not clicking
through a prompt.

**`/pause` is not that.** Pausing does not deny anything; it takes Halyard out of the loop. The
hook returns no opinion, and Claude Code then decides exactly as it would if the hook had never
been installed — which means its own `permissions.allow` list decides. Anything that list covers
runs with no prompt, no card, and no audit entry, and everything else it asks you at the desk.

Both halves of that are worth knowing. It is why pausing from your phone is safe to do while you
are away from the machine, and it is why a long `permissions.allow` list means pausing lets more
through than you might picture.

Two practical habits follow:

- **Do not wire the gate into the repository you fix Halyard from.** If the control plane breaks
  and the only place you can run commands is behind the gate it is holding shut, you have locked
  yourself out of the repair.
- **Keep a Telegram client where notifications actually reach you.** An approval expires after
  `HALYARD_APPROVAL_TIMEOUT_SECONDS` and is then denied. A browser tab you closed is not a client.

**Claude Code snapshots hook configuration at startup.** Editing `settings.json` mid-session has no
effect; restart the session. The script contents are read on every call, so those can change freely.


---

Next: [set up the Telegram side](telegram.md) — the bot, and where each seat's
traffic lands.

[← Back to the README](../README.md)
