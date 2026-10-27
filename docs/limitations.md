# Known limitations

What Halyard does not do, or does not do well yet — worth reading before you rely
on it.

- **The desktop apps show an injected turn late, not never.** A message from your
  phone reaches the session and its reply comes back to you. In the Claude Code
  app, Reload shows it, without interrupting a turn that is running.
- **Two things can outrun the gate.** A hook that exceeds its timeout, and a wrapper
  that cannot start at all, both let the command through. `doctor` checks for the
  second.
- **A ZCode agent stops at a captcha.** Messages reach a ZCode session and its gate
  answers from your phone, but its provider asks for a captcha from time to time,
  and the token a solved one produces can only be got in ZCode's own window.
  Halyard says so and ends the turn rather than waiting on it; you solve it at the
  desk and send again. If you would rather not meet that at all, drive the agent
  with another runtime — opencode runs the same models and is gated the same way.
- **One bot token per machine.** Telegram's `getUpdates` has a single consumer.
- **`/commit` reads the whole working tree, not the staging area.** Agents write
  code and stage nothing, so a control plane that answered "nothing is staged"
  would be refusing the only thing it was asked for. What `.gitignore` excludes
  is excluded, and the card names the files that are new.
- **The project's own checks belong to its commit options, not to the plain
  Commit.** `commit_options:` names commands and inspections from the project's
  own lists, and `/commit` offers each option as a button; a failing command
  means no card at all rather than a question nobody can usefully answer. The
  plain Commit writes a message and stops, which is what most changes want, and
  it commits the tree as it is at the tap rather than the one the card showed.
- **Agents can be stopped from committing at all.** `HALYARD_REFUSE_AGENT_COMMITS`
  refuses an agent's own `git commit` or `git push` before anybody is asked. Off
  by default. Unlike everything else the gate does, `/pause` does not lift it: a
  pause means "stop asking me", and a guard a pause switches off is a guard
  nobody can rely on.
- **`/pause` steps aside rather than denying.** The runtime's own permission list
  then decides, with no card and no audit entry.
- **A chat that approves for itself is left to it.** Codex's "approve for me" and
  Claude Code's auto mode were chosen in the chat, so their calls are not asked
  again on the phone: the runtime's own reviewer decides, and the audit log says
  `call.left_to_agent`. Refusals such as `HALYARD_REFUSE_AGENT_COMMITS` still
  apply, and Halyard's own turns are always asked. `HALYARD_OVERRIDE_AGENT_APPROVALS:
  true` puts every call back on the gate; `halyard doctor` says which way it is.
- **The gate covers what the matcher covers** — Bash, the file tools, the
  question tool, MCP calls, `WebFetch` and `WebSearch`. Not `Read`, `Grep` or
  the client's own tool loading, which fire too and would put a card in front of
  every file read. Widening it also widens what stops when Halyard is down:
  those tools are denied then, exactly as Bash is.
- **Multiple-choice questions reach you from a desktop or terminal session, not from a
  turn you started over Telegram.** A Telegram-initiated turn runs headless, where
  Claude Code does not load `AskUserQuestion` — so it asks in prose you can just reply
  to, rather than as buttons.
- **An answer to a Codex question waits for the turn running in its thread.** Codex
  carries on after asking, and Halyard hands the answer in with `codex queue`, which
  cannot steer a turn that is running: Codex takes it when that turn ends, and the card
  says the answer is queued. Pressing Steer in the app hands it over at once. Steering
  it from Halyard would need Codex's shared app-server to hold the thread, which the
  Desktop app's threads may not; it is not built.
- **Only Codex says how full its limits are.** It writes its accounting into its
  own transcript on every turn, so `/status` can read it back. Claude Code
  publishes nothing to read — measured on 2.1.246: no usage command on the CLI,
  and per-turn token counts with no limit anywhere among them — so its agents show
  no standing rather than a guess.
- **Nothing reports when the control plane's own token expires.** `claude auth
  status` answers with eight fields and not one of them is a date, and the token
  itself is opaque. So Halyard notes when it first saw the token and says so a
  month before the year is up, in words that admit it is an estimate. Minting a
  replacement starts the clock again on its own; the token is never written down,
  only a salted derivation of it.
- **Only macOS has been run.** Nothing here is deliberately macOS-only and the
  Linux paths exist, but they have not been exercised: the CLIs are found by
  looking in places a Mac keeps them, and holding the machine awake uses
  `caffeinate`, which simply does nothing elsewhere. Windows is untried
  entirely. If you run it somewhere else, `halyard doctor` is the first thing
  to read and the place a wrong assumption will show up.
- **Antigravity is not shipped.** The adapter works — it gates, it delivers, it
  brings replies back — but a command approved from your phone is still approved a
  second time in the desktop app whenever the agent asks to leave the sandbox.
  Antigravity ignores a hook that tries to grant that, deliberately: honouring it
  would let whoever controls the hook run unsandboxed code on the machine. That is
  the right call, so the runtime waits rather than shipping with a second prompt.
  Everything found along the way is in the
  [Antigravity notes](antigravity-payload-notes.md).

---

[← Back to the README](../README.md)
