# Upkeep: what Halyard does about itself

Everything else in Halyard is about somebody's project. `upkeep` is about
Halyard: jobs that read what it has kept — the audit log, the cards it asked —
and put that in front of a model with a question. Looking after the gate then
does not mean writing a program each time a new question about it comes up.

Halyard is not a project here, and nothing is wired. A job has a block of its
own in `halyard.yaml`, answers in the terminal, and changes nothing.

## runs-advice

Which commands a project could trust to run without a card, from the cards it
still asks.

```bash
make runs-advice p=alpha-engine          # a model proposes; you add what you agree with
make runs-advice p=alpha-engine e=1      # only the evidence, and no model
uv run halyard upkeep runs-advice alpha-engine --days 30
```

**Halyard gathers the evidence.** From its log, over the last 14 days:
- the shell commands that came to your phone as cards, and how each ended —
  allowed, denied, timed out, never answered;
- what today's rules would make of each: through without a card, still a card,
  refused outright, or undetermined. It uses the service's own rules, this
  machine's settings, and the project's `runs:` and `halyard rules` entries.
- what the project trusts now.

A command that redaction changed cannot be judged again from the log, and is
counted as undetermined. A card recorded before 2026-09-25 does not say where it
ran; it is judged from the project's root and counted as approximate. The
families still asking are listed most first, bounded, with what was left out
counted.

**A model reads it and proposes.** It gets the evidence and nothing else. The
turn has no tools: no files, no commands, no MCP server. So it cannot open
`halyard.yaml`, which holds the bot token, or the database. It is asked for entries and a
reason for each, never for counts or commands.

**Halyard checks and counts every proposal.**
- An entry that could run anything is refused, as it would be in `runs:`.
- Each proposal shows how many cards in the window it would have spared, and
  how many of those were denied, timed out or never answered.
- Each comes with the line that adds it, quoted by Halyard:

```
  uv run pytest *
    would have spared 868 of the cards — 4 of them were denied, timed out or never answered
    why: the project's test runner; its arguments are test paths and -k expressions
    halyard rules add alpha-engine 'uv run pytest *'
```

Nothing is added by the job. You run the lines you agree with.

**Every run is kept.** A row in `upkeep_runs` in `halyard.db` holds:
- the evidence;
- the model's answer;
- what Halyard made of it, checked and counted;
- what you were shown.

The row's id is the one its turn ran under, which is `turn_usage.session_id` for the same turn. A run that got no answer, or one of the wrong shape, is kept too.

```bash
uv run halyard upkeep recent              # the last runs, with their ids
uv run halyard upkeep show 3f2a9c1e       # what one printed, again
uv run halyard upkeep show 3f2a --evidence   # and what it was given
```

**It works with Halyard stopped.**
- It reads the database over a read-only connection, closed before the model
  is asked. It writes the run's own row only once the turn is over.
- It never talks to the running service, never waits for a card, and ends when
  the model fails or runs out of time.
- What the turn used is recorded like any turn Halyard starts: `upkeep
  runs-advice` in `halyard usage`.

The turn needs a runtime that can promise it runs without tools. Claude Code
can. Codex and opencode cannot, and are not asked instead.

## Configuration

```yaml
upkeep:
  runs-advice:
    model: opus          # HALYARD_INSPECTION_MODEL when unset
    effort: high         # HALYARD_INSPECTION_EFFORT when unset
    days: 14
    timeout: 600         # seconds the model may take
    prompt: my-advice.md # in place of the one that ships, relative to halyard.yaml
```

All of it is optional; without the block the job runs on the defaults.

---

[← Back to the README](../README.md)
