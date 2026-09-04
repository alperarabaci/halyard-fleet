"""What an agent is never allowed to do, whoever is asked.

The mirror of `writes.py` and `tools.py`. Those say *do not ask, allow*; this
says *do not ask, refuse* — and the gate has had no way to say that until now.
Everything else it decides is a question put to a person; this is a standing
answer given before anybody is asked.

One rule today, off by default: an agent may not commit or push. Halyard commits
on request from a phone, with the diff summarised and a message to approve, and
that is a different act from an agent deciding on its own that now is the moment
to write history. Somebody who wants their agents committing leaves the flag
alone and nothing changes.

**`/pause` does not lift it.** Pausing means "stop asking me", and it hands each
call back to the runtime's own permission list — which is the right answer for a
question, and the wrong one for a rule. A guard that a pause quietly switches
off is a guard nobody can rely on, so this is checked first of all.

**It is a guardrail, not a lock.** An agent that wanted to could write a script,
add an alias, or call a library. This stops the reflex — the agent that helpfully
commits because that is what one does after making a change — and it does not
pretend to stop anything determined.
"""

from __future__ import annotations

import shlex

#: Words that stand in front of a command without being it.
_PREFIXES = frozenset({"sudo", "command", "nohup", "time", "env"})

#: Where one command ends and the next begins, as `shlex` hands them over.
#: Newlines are not here — they are split off before lexing, because `shlex`
#: reads one as ordinary whitespace.
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})

#: git's own options that take a value, so the value is not read as the
#: subcommand — `git -C /somewhere commit` must not look like `git /somewhere`.
_TAKES_A_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})

#: What an agent must not do.
_ACTS = frozenset({"commit", "push"})


def writes_history(command: str) -> str | None:
    """`"commit"`, `"push"`, or None if this command does neither.

    Tokenised rather than matched. The first version of this was a regular
    expression and it was wrong twice over: it read `echo git commit` as a
    commit, and CodeQL found it could be made to backtrack exponentially on a
    string like `-c -0 -c -0 …`. A command line arriving from an agent is
    exactly the input nobody should hand to an ambiguous pattern, and a gate
    that can be made to hang is a gate that stops delivering approval cards.

    `shlex` also settles the quoting for free: `echo "then run git commit"` is
    one word to it, so documentation about committing stays documentation.
    """
    for words in _commands_in(command):
        act = _act_of(words)
        if act is not None:
            return act
    return None


def _commands_in(command: str) -> list[list[str]]:
    """The command line split into the commands it actually runs.

    A parse failure is treated as one unsplittable command rather than as
    nothing: unbalanced quotes are not a way past this.
    """
    found: list[list[str]] = [[]]
    # Lines first, because `shlex` reads a newline as ordinary whitespace — so
    # a two-line script would be one command whose first word is `make`, and the
    # `git commit` on its second line would go unseen. Agents send multi-line
    # bash routinely.
    for line in (command or "").splitlines():
        try:
            # `shlex.split` cannot be told about punctuation, and the separators
            # are the point here: without them `echo hi; git commit` is one
            # command whose first word is `echo`.
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # Malformed, so read coarsely and let `_act_of` decide. Being
            # generous here would make a stray quote a way around the rule.
            tokens = line.split()
        found.append([])
        for token in tokens:
            if token in _SEPARATORS:
                found.append([])
            else:
                found[-1].append(token)
    return found


def _act_of(words: list[str]) -> str | None:
    """Which act this one command is, if it is git doing either of them."""
    index = 0
    while index < len(words) and words[index] in _PREFIXES:
        index += 1
    # `env FOO=1 git …` and the like.
    while index < len(words) and "=" in words[index] and not words[index].startswith("-"):
        index += 1
    if index >= len(words) or words[index] not in {"git", "/usr/bin/git"}:
        return None

    index += 1
    while index < len(words) and words[index].startswith("-"):
        takes_value = words[index] in _TAKES_A_VALUE
        index += 1
        if takes_value:
            index += 1
    if index >= len(words):
        return None
    return words[index] if words[index] in _ACTS else None


def writes_history_if(command: str, refusing: bool) -> str | None:
    """Which act this is, when refusing is switched on. None otherwise.

    Two arguments rather than a check at the call site, so the flag and the
    pattern are read in one place and a caller cannot accidentally apply one
    without the other.
    """
    return writes_history(command) if refusing else None


def why(act: str) -> str:
    """What the agent is told, which is not the same as what is recorded.

    It says what to do instead. An agent told only "denied" tries the next
    spelling of the same command; one told who does commit here stops and says
    so to the person.
    """
    return (
        f"Refused: agents do not {act} in this project. "
        "Halyard commits on request from the phone — ask for it and it will be offered "
        "with the diff and a message to approve. Do not try another way of running this."
    )
