"""Sending a message into a running Claude Code session.

`claude -p --resume <session_id> "<text>"` continues the *same* session: same
id, same transcript, context intact. Measured — a session told to remember a
number answered with it from a separate process, and four turns issued from
four processes were afterwards recalled as one conversation. See
`docs/session-io-notes.md`.

That is what makes this different from a bot that keeps its own thread. A
message typed on a phone lands in the session itself, so whoever opens that
conversation later sees it in the history like any other turn.

**One writer at a time.** Two overlapping resumes of one session do not fail —
they fork silently, and one of them is simply absent from the conversation
afterwards. Nothing errors and the transcript still parses. So sends are
serialised per session here, and the session Halyard writes to should not also
be one somebody is typing into.

Runs on the host, not in a container: it needs the `claude` binary and the
credentials in the user's home directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

#: How long to wait for a turn before giving up. Generous, because a real turn
#: runs tools and can take minutes — each of which may stop for its own
#: approval, which is a human deciding on a phone.
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

#: What `--effort` accepts. A closed set the CLI documents, so a typo can be
#: caught here rather than by a turn that fails a minute later.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: Model aliases the CLI names. A hint, not a gate — new models ship faster than
#: this project does, and rejecting one because it is not on a list written
#: months ago would be worse than passing it through and letting the CLI answer.
#: Override with HALYARD_CLAUDE_MODELS when something new appears.
DEFAULT_MODELS = ("opus", "sonnet", "haiku", "fable")

#: What a turn runs on when nobody has said otherwise.
#:
#: An opinion, and deliberately not the CLI's. `claude -p` with no `--model`
#: uses haiku — measured, not read — which is a fine default for a one-shot
#: prompt and the wrong one for continuing work on a real codebase. Left alone,
#: every message sent from a phone would quietly run on the cheapest model
#: available while the session it landed in was set to something else, and
#: nothing in the reply would say so.
DEFAULT_MODEL = "sonnet"

#: Where the CLI usually is when PATH does not have it, which is the common case
#: for a service started outside a login shell.
_FALLBACK_BINARIES = (
    Path.home() / ".local" / "bin" / "claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)


def find_claude_binary(configured: str | None = None) -> str | None:
    """Locate the CLI, preferring an explicit setting."""
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _FALLBACK_BINARIES:
        if candidate.exists():
            return str(candidate)
    return None


class ClaudeCodeRunner:
    """Delivers a message into a Claude Code session by resuming it."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        models: tuple[str, ...] | None = None,
        default_model: str | None = DEFAULT_MODEL,
    ) -> None:
        self._known_models = models or DEFAULT_MODELS
        self._default_model = default_model or None
        self._binary = find_claude_binary(binary)
        self._timeout = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Per-session overrides for turns *this* runner starts. A turn begun at
        # a keyboard uses whatever the app is set to; nothing here can reach it.
        self._models: dict[str, str] = {}
        self._efforts: dict[str, str] = {}

    @property
    def id(self) -> str:
        return "claude-code"

    @property
    def available(self) -> bool:
        """Whether the CLI could be found at all.

        False in a container, which has no binary and no credentials — worth
        reporting plainly rather than discovering it on the first message.
        """
        return self._binary is not None

    def options(self) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen, as {name: (values, whether it is enforced)}.

        The adapter answers for itself so the channel can print it without
        knowing anything about a runtime. A second adapter — Codex, whatever
        comes after — replies with its own, and the command that shows this
        needs no change to cover it.

        Models are a hint: a name not on the list is still passed through,
        because a list written months ago should not be able to refuse a model
        that shipped this morning. Effort is enforced, because the CLI
        documents a closed set.
        """
        return {"model": (self._known_models, False), "effort": (EFFORT_LEVELS, True)}

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        """The model and effort this runner will use for that session.

        What will actually happen, not what was typed — so the model reported
        here is the configured default until somebody overrides it. Reporting
        the override alone would print nothing in the ordinary case and leave
        the real answer to be guessed.
        """
        return self._models.get(session_id) or self._default_model, self._efforts.get(session_id)

    def set_model(self, session_id: str, model: str | None) -> None:
        """Choose the model for turns started from a channel. None clears it."""
        if model:
            self._models[session_id] = model
        else:
            self._models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
        """Choose the reasoning effort. None clears it."""
        if effort:
            self._efforts[session_id] = effort
        else:
            self._efforts.pop(session_id, None)

    def busy(self, session_id: str) -> bool:
        """Whether a turn this runner started is still going in that session.

        Only what Halyard itself is doing — a turn somebody started at the desk
        is invisible from here, and claiming otherwise would be worse than
        saying nothing.
        """
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        """Resume the session with `text` as the next thing the user said.

        `cwd` is the directory the session belongs to. It matters: `--resume`
        looks for a conversation within the current project, so running it
        from anywhere else answers "No conversation found with session ID"
        even though the transcript is right there on disk.
        """
        if not self._binary:
            logger.error(
                "Cannot deliver a message: the claude CLI was not found. "
                "The control plane has to run on the host for this, not in a container."
            )
            return False
        if not text.strip():
            return False

        # Per session, so two messages to one conversation queue instead of
        # racing, while two different sessions still run at the same time.
        async with self._locks[session_id]:
            return await self._run(session_id, text, cwd)

    async def _run(self, session_id: str, text: str, cwd: str | None) -> bool:
        try:
            arguments = [self._binary, "-p", "--resume", session_id]
            if model := self._models.get(session_id) or self._default_model:
                arguments += ["--model", model]
            if effort := self._efforts.get(session_id):
                arguments += ["--effort", effort]
            arguments.append(text)

            process = await asyncio.create_subprocess_exec(
                *arguments,
                # Closed rather than inherited: a resumed run warns and stalls
                # for three seconds when it is handed a stdin that never
                # produces anything.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=os.environ.copy(),
            )
        except OSError:
            logger.exception("Could not start the claude CLI")
            return False

        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            logger.error("A turn in %s ran past %.0fs; giving up on it", session_id, self._timeout)
            process.kill()
            await process.wait()
            return False

        if process.returncode != 0:
            logger.error(
                "Delivering a message to %s failed (exit %s): %s",
                session_id,
                process.returncode,
                (stderr or b"").decode("utf-8", "replace").strip()[:400],
            )
            return False

        # The reply is not read from here. It arrives the same way every other
        # turn's does — through the Stop hook and the relay — so a message sent
        # from a phone and one typed at the desk come back by one path.
        return True
