# Before you wire it into anything

Halyard takes over your agent's permission prompt. Everything below is a
consequence of that, and every line was measured rather than read — usually
after it caused a problem.

None of these announce themselves at the moment they bite. A denied `ls` looks
like a broken tool, a paused gate looks like a closed one, an expired approval
looks like a command that hung. Nobody should discover them afterwards.

**1. A wired project needs the control plane running.** From the moment the hook is in
`settings.local.json`, a Bash command with Halyard down is *denied* — every one of them, `ls`
included — and there is no terminal prompt to approve it with. Wiring is not something you do once
and forget; it is something the project now depends on. Turning it back off is a command, and it
keeps a backup:

```bash
halyard wire ~/code/my-project     # backs up, then merges each runtime's hooks
halyard unwire ~/code/my-project   # removes only Halyard's hooks, leaves the rest alone
```

**2. It is live from the first command.** There is no arming step. Once the control plane is up and
the hook is wired, approvals go to Telegram immediately. `/pause` is what stops that — and pausing
needs the control plane running too, because the pause switch lives inside it.

**3. An approval expires.** `HALYARD_APPROVAL_TIMEOUT_SECONDS` (300s by default) and then it is
*denied*, not left waiting. A phone with notifications muted is the same as answering no.

### Things that surprised us, kept here so they do not surprise you

Every line below was measured rather than read, usually after it caused a problem.

- **`/pause` does not deny anything — it steps aside.** Claude Code then decides exactly as if the
  hook were never installed, which means its own `permissions.allow` list runs matching commands
  with no prompt, no card, and no audit entry. Safe to pause while you are away; worth knowing that
  a long allow list means more goes through than the word "paused" suggests.
- **Hooks are read once, at session start.** Editing settings mid-session changes nothing until you
  restart the session. Script *contents* are re-read every call, so those can change freely.
- **`settings.local.json` is not yours alone.** Claude Code appends every "don't ask again" to a
  `permissions.allow` list in the same file, and it is gitignored. Never write that file wholesale —
  use `halyard wire`, which merges.
- **The gate covers what the matcher covers.** With `"matcher": "Bash"`, `Write` and `Edit` are not
  gated at all.
- **The project root is the git root.** A session opened in a subdirectory is gated by the `.claude/`
  at the top of its repository — and by nothing, if there is no repository above it.
- **One bot token per machine.** Telegram's `getUpdates` has a single consumer; two control planes
  sharing a token will steal each other's messages.
- **Do not wire the gate into the repository you repair Halyard from.** If the only place you can
  run commands is behind the gate that is stuck shut, the repair is behind it too.
- **Do not type into a session Halyard is writing to.** Two overlapping resumes of one session do
  not fail — they fork silently, and one side's history simply disappears.
- **The desktop app shows an injected turn late, not never.** A message you send from the phone is
  delivered into the session, processed, and written to the transcript — it is a real part of the
  conversation, and the reply comes back to your chat. But the desktop app does not live-refresh an
  open session while an external process appends to it; it catches up when the window is focused
  again, and a restart always shows everything. This is not a problem for what Halyard is *for* —
  you are away from the machine, driving from the phone — and it costs nothing there. It only shows
  as a lag when you happen to be watching the desktop at the same time.

Found something in this category that is not on the list? It belongs here, or it belongs fixed.
Open an issue either way.

---

[← Back to the README](../README.md)
