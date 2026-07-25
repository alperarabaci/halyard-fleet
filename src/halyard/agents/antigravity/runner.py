"""Sending a message into an Antigravity conversation.

**There are two Antigravities, and they share nothing.** The desktop
application keeps its conversations under `~/.gemini/antigravity/`; the `agy`
CLI keeps its own under `~/.gemini/antigravity-cli/`, with a parallel `brain/`
and `conversations/`. A conversation started in one is invisible in the other —
measured, and it is why a CLI session does not appear in the application the
way a Codex one does.

**Only conversations the application owns can be addressed.** `agy --help`
describes `--conversation <id>` as "Resume a previous conversation by ID" and
it is not one: measured three times, it starts a *new* conversation seeded with
a summary of the one named, leaving the original untouched. The database is the
proof — the referenced conversation kept its 13 steps and never saw the text,
the new one recorded 4, and `parent_references` was empty in both, so the two
are not even linked. `conversation_summaries.db` beside them is what the flag
actually reads.

That makes CLI delivery worse than no delivery. A message that fails is
visible; one that silently forks is answered somewhere nobody is watching while
the conversation a seat names sits there looking idle. So a CLI conversation is
refused by name, and the only path is the one that appends where it says it
does:

    agentapi send-message <id> "<text>"      a conversation the application owns

It needs the application running, and finds it by reading a third process's
command line:

    ANTIGRAVITY_LS_ADDRESS   the language server's gRPC port
    ANTIGRAVITY_CSRF_TOKEN   the `--csrf_token` argument it was started with

Neither is published to a file. This is not a contract, it is what is
available, and it will break without notice — so every lookup is fresh, every
failure is reported rather than cached, and `available` means "the app is up
right now" rather than "a binary exists".

**A send can queue.** `send-message` returns success while Antigravity is
waiting for a human to answer a permission prompt, and the queued messages all
arrive at once when they answer. Measured: three sends, thirty minutes apart in
effect, all delivered together. So a return value here means accepted, never
processed — which is why the reply comes back through the Stop hook like every
other runtime's rather than being read from this call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from halyard.agents.antigravity.sessions import find_session, is_cli

logger = logging.getLogger(__name__)

DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

#: The application bundles its own binary; nothing lands on PATH.
_BINARY = Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server")
_SHIM = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"

#: The tiers `agentapi` takes, for a conversation the application owns. The
#: only vocabulary here: `agy` names real models and takes an `--effort`, but
#: nothing can be sent to the conversations it owns, so offering its words
#: would be offering a choice that cannot be acted on.
APP_MODELS = ("flash_lite", "flash", "pro")


#: What a delivered message is labelled as, and it cannot be changed.
#:
#: `agentapi send-message` is an agent-to-agent notification channel — "Send
#: messages to another conversation or yourself" — so Antigravity files every
#: one of them as a `SYSTEM_MESSAGE`, prefixed with its own sentence saying the
#: user did not send it. Measured: `--title`, the only flag it takes, changes
#: nothing in the envelope, which arrives as
#:
#:     [Message] timestamp=... sender=system priority=MESSAGE_PRIORITY_HIGH content=...
#:
#: `sender=system` is fixed. The other two runtimes deliver a genuine user turn
#: and this one has no interface that can, so the identity goes in the one
#: field that *is* ours — the content. Short and factual on purpose: it says
#: where the message came from without instructing the model what to do about
#: it, because a person typing on a phone did not ask for their sentence to be
#: argued with before it arrives.
SENT_BY = "[Telegram]"


def as_a_person(text: str) -> str:
    """Mark a message as having come from a person rather than the system."""
    return f"{SENT_BY} {text}"


def find_antigravity_binary(configured: str | None = None) -> str | None:
    for candidate in (Path(configured) if configured else None, _BINARY, _SHIM):
        if candidate and candidate.exists():
            return str(candidate)
    return None


def language_server_endpoints() -> list[tuple[str, str]]:
    """Every address the running language server might be reached at, with its token.

    Read from the process table because there is nowhere else to read it from,
    and returned as a list because it listens on more than one port and only
    one of them speaks gRPC — the others answer `error reading server preface`.
    Which is which is not knowable from the outside, so they are tried in turn
    rather than guessed.
    """
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    for line in listing.splitlines():
        if "Antigravity.app" not in line or "language_server" not in line:
            continue
        token = re.search(r"--csrf_token\s+(\S+)", line)
        if not token:
            continue
        pid = line.split(None, 1)[0]
        try:
            ports = subprocess.run(
                # `-a` because lsof ORs its filters otherwise. Without it this
                # returns every listener on the machine, and it produced a
                # confident endpoint pointing at an unrelated application.
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        found = re.findall(r"(127\.0\.0\.1:\d+)\s+\(LISTEN\)", ports)
        # Unsorted. It listens on several and only one speaks gRPC; which one
        # is not predictable — measured across two runs, the working port was
        # neither the highest nor the lowest — so the caller tries them and
        # remembers the answer instead of ordering on a guess.
        return [(address, token.group(1)) for address in found]
    return []


class AntigravityRunner:
    """Delivers a message into an Antigravity conversation."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._binary = find_antigravity_binary(binary)
        self._timeout = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._models: dict[str, str] = {}
        # The endpoint that last worked. Ports move when the application
        # restarts, so this is a shortcut rather than a fact: it is tried
        # first and discarded the moment it fails.
        self._endpoint: tuple[str, str] | None = None

    @property
    def id(self) -> str:
        return "antigravity"

    @property
    def available(self) -> bool:
        """Whether a message could be delivered *now*.

        Stricter than the other runtimes, and honestly so: theirs need a binary,
        this one needs a running application, and reporting it available while
        the app is shut would turn a knowable failure into a surprise.
        """
        return self._binary is not None and bool(language_server_endpoints())

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen: three tiers, and no reasoning effort at all.

        The same whichever conversation is asked about. `agy` does offer more —
        real model names and an effort — but only for conversations that cannot
        be delivered to, and a menu whose choices apply to nothing reachable is
        a worse answer than a short one.
        """
        return {"model": (APP_MODELS, False), "effort": ((), True)}

    def resolve(self, name: str):
        return find_session(name)

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        # The effort is always None. See `set_effort`.
        return self._models.get(session_id), None

    def set_model(self, session_id: str, model: str | None) -> None:
        if model:
            self._models[session_id] = model
        else:
            self._models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
        """Accepted and dropped: the application has no reasoning effort.

        Not stored, deliberately. Keeping it would make `preferences()` report a
        setting back that nothing on the delivery path ever applies, and a
        runtime that agrees to a choice it silently ignores is harder to catch
        than one that never claimed to have it. Accepting the call at all is so
        a channel can offer one command across every runtime; `options()` is
        where the honest answer lives, and it reports none.
        """

    def busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        """Put `text` into the conversation. Returns whether it was accepted.

        `cwd` is unused: `send-message` addresses a conversation directly and
        the application already knows where that conversation lives. Kept in the
        signature because the protocol has it and the other two runtimes need it.
        """
        if not text.strip():
            return False

        # A conversation the `agy` CLI owns has no way in. Refused here rather
        # than attempted, because the attempt succeeds: `--conversation` forks
        # instead of resuming, so the text would be answered in a conversation
        # nobody is reading while this returned True.
        if is_cli(session_id):
            logger.error(
                "Conversation %s belongs to the agy CLI, which cannot be sent to. "
                "The CLI keeps its conversations in a separate store the application "
                "cannot see, and `agy --conversation` starts a new conversation rather "
                "than continuing the one named. Use a conversation opened in the "
                "Antigravity application.",
                session_id,
            )
            return False

        if not self._binary:
            logger.error("Cannot deliver: Antigravity is not installed on this machine.")
            return False

        endpoints = language_server_endpoints()
        if not endpoints:
            logger.error(
                "Cannot deliver to %s: Antigravity is not running. Unlike the other "
                "runtimes it has no CLI of its own — messages go through the open "
                "application.",
                session_id,
            )
            return False

        # Whatever worked last, then everything else. A wrong guess costs one
        # subprocess that fails immediately; discovering afresh every time
        # costs one per candidate port on every message.
        ordered = endpoints
        if self._endpoint in endpoints:
            ordered = [self._endpoint] + [e for e in endpoints if e != self._endpoint]

        async with self._locks[session_id]:
            for endpoint in ordered:
                if await self._run(session_id, text, endpoint):
                    self._endpoint = endpoint
                    return True
            self._endpoint = None
            logger.error(
                "No Antigravity endpoint accepted a message for %s; tried %s",
                session_id,
                ", ".join(address for address, _ in ordered),
            )
            return False

    async def _run(self, session_id: str, text: str, endpoint: tuple[str, str]) -> bool:
        address, token = endpoint
        arguments = [self._binary, "agentapi", "send-message"]
        arguments += [session_id, as_a_person(text)]

        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PATH": "/usr/bin:/bin",
                    "ANTIGRAVITY_LS_ADDRESS": address,
                    "ANTIGRAVITY_CSRF_TOKEN": token,
                },
            )
        except OSError:
            logger.exception("Could not start agentapi")
            return False

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            logger.error("Delivering to %s ran past %.0fs", session_id, self._timeout)
            process.kill()
            await process.wait()
            return False

        if process.returncode != 0:
            # Debug rather than error: with several candidate ports, the ones
            # that are not gRPC fail first and normally, and logging each as a
            # failure would bury the one that matters.
            logger.debug(
                "agentapi at %s did not accept a message for %s (exit %s): %s",
                address,
                session_id,
                process.returncode,
                (stderr or stdout or b"").decode("utf-8", "replace").strip()[:200],
            )
            return False

        # agentapi answers 200 with an error inside the body often enough that
        # the exit code alone is not the answer.
        try:
            answer = json.loads(stdout or b"{}")
        except ValueError:
            answer = {}
        if answer.get("error"):
            logger.error("agentapi refused a message to %s: %s", session_id, answer["error"])
            return False
        return True
