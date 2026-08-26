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
import json
import logging
import os
import re
import shutil
import subprocess
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

#: No model override by default. Measured on a live Desktop-owned session:
#: `--resume` with no `--model` continued on that session's opus model. The
#: earlier haiku measurement came from a fresh headless prompt and was wrongly
#: applied to resumed sessions. Supplying a default here broke that inheritance.
DEFAULT_MODEL: str | None = None

#: Where the CLI usually is when PATH does not have it, which is the common case
#: for a service started outside a login shell.
_FALLBACK_BINARIES = (
    Path.home() / ".local" / "bin" / "claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)

# Claude Desktop ships the Claude Code engine it uses for its open sessions.
# Running a different installed CLI version against a session owned by the app
# has regressed live transcript refresh in practice. Prefer the app's engine on
# macOS so the external resume writer and the desktop reader speak the same
# version. HALYARD_CLAUDE_BINARY remains an explicit escape hatch.
_DESKTOP_CLAUDE_CODE_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
)


def _desktop_claude_binary() -> str | None:
    """Return the newest Claude Code engine bundled with Claude Desktop."""

    def version_key(binary: Path) -> tuple[int, ...]:
        version = binary.parents[3].name
        return tuple(int(part) for part in re.findall(r"\d+", version))

    candidates = [
        candidate
        for candidate in _DESKTOP_CLAUDE_CODE_DIR.glob("*/claude.app/Contents/MacOS/claude")
        if candidate.is_file()
    ]
    if not candidates:
        return None
    return str(max(candidates, key=version_key))


def signed_in(binary: str | None = None) -> bool | None:
    """Whether the CLI can actually authenticate. `None` when it cannot be told.

    `claude auth status` answers in about a third of a second and costs no
    turn — measured — so there is no reason for `doctor` not to ask. It prints
    JSON with a `loggedIn` field.

    This is the gap that let a Mac mini look healthy and deliver nothing. The
    binary was present, `doctor` reported it, and every message failed with the
    CLI's own "Not logged in · Please run /login" — which was being written to
    stdout and thrown away. A binary that exists and cannot sign in is not a
    runtime you can send to.

    Three states, not two. `None` means the question could not be asked — an
    old CLI without the subcommand, a timeout — and is reported as unknown
    rather than as a failure. A checker that is confidently wrong is worse than
    one that states its limit: the obvious response to a false negative here is
    to go and re-authenticate something that was never signed out.
    """
    found = find_claude_binary(binary)
    if found is None:
        return None
    try:
        done = subprocess.run([found, "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return bool(json.loads(done.stdout or "{}").get("loggedIn"))
    except ValueError:
        return None


def auth_method(binary: str | None = None) -> str | None:
    """Which credential the CLI would use, as it names it, or None if unasked.

    Worth reporting separately from *whether* it is signed in. The two failures
    look identical from outside and are fixed differently: a login that expired
    needs somebody at the keyboard, and a control plane running on the desktop
    login needs a token so that it stops needing one.

    `auth status` carries no expiry, measured on 2.1.246 — `loggedIn`,
    `authMethod`, `apiProvider` and nothing about time — so nothing here can
    warn ahead of the event. Naming the method is what is available.
    """
    found = find_claude_binary(binary)
    if found is None:
        return None
    try:
        done = subprocess.run([found, "auth", "status"], capture_output=True, text=True, timeout=15)
        method = json.loads(done.stdout or "{}").get("authMethod")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return method if isinstance(method, str) and method else None


def find_claude_binary(configured: str | None = None) -> str | None:
    """Locate the CLI, preferring an explicit setting then Desktop's engine."""
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    if desktop := _desktop_claude_binary():
        return desktop
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
        oauth_token: str | None = None,
    ) -> None:
        self._known_models = models or DEFAULT_MODELS
        self._default_model = default_model or None
        # A credential of our own for the turns this runner starts, so they do
        # not ride on the desktop login that expires while nobody is at the
        # keyboard. Never logged: it reaches the CLI through the environment of
        # one subprocess and appears in no argument list.
        self._oauth_token = (oauth_token or "").strip() or None
        # The path is *not* resolved here. A control plane runs for days, and
        # what it can reach changes underneath it: a CLI installed after
        # startup stayed invisible until a restart, and Claude Code's binary
        # lives under a version number, so an upgrade moves it and leaves a
        # long-running process pointing at a path that no longer exists.
        # Measured: `doctor` found codex and the runner did not, in the same
        # minute, because one had asked at startup and the other just now.
        self._configured = binary
        self._timeout = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_error: dict[str, str] = {}
        # Per-session overrides for turns *this* runner starts. A turn begun at
        # a keyboard uses whatever the app is set to; nothing here can reach it.
        self._models: dict[str, str] = {}
        self._efforts: dict[str, str] = {}

    @property
    def id(self) -> str:
        return "claude-code"

    @property
    def _binary(self) -> str | None:
        """Where the CLI is, asked now rather than remembered."""
        return find_claude_binary(self._configured)

    @property
    def available(self) -> bool:
        """Whether the CLI could be found at all.

        False in a container, which has no binary and no credentials — worth
        reporting plainly rather than discovering it on the first message.
        """
        return self._binary is not None

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen, as {name: (values, whether it is enforced)}.

        The adapter answers for itself so the channel can print it without
        knowing anything about a runtime. A second adapter — Codex, whatever
        comes after — replies with its own, and the command that shows this
        needs no change to cover it.

        Models are a hint: a name not on the list is still passed through,
        because a list written months ago should not be able to refuse a model
        that shipped this morning. Effort is enforced, because the CLI
        documents a closed set.

        `session_id` is ignored here — Claude Code accepts the same five effort
        levels whatever the model. It is in the signature because Codex does
        not, and one protocol answering for both beats two protocols.
        """
        return {"model": (self._known_models, False), "effort": (EFFORT_LEVELS, True)}

    def resolve(self, name: str):
        """Find a session by its name in the app, or by its id."""
        from halyard.agents.claude_code.sessions import find_session

        return find_session(name)

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

    def last_error(self, session_id: str) -> str | None:
        """Why the last delivery to this session failed, if one did.

        Kept so the answer can travel to the person who asked. They are on a
        phone, away from the machine, and "check the control plane's log" is
        the one instruction they cannot follow — the log is on the machine they
        are away from. The reason was already printed by the CLI; it only had
        to be carried.
        """
        return self._last_error.get(session_id)

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

    async def ask(
        self, text: str, *, timeout: float = 180.0, model: str | None = None
    ) -> str | None:
        """Run one prompt in a session of its own and return what came back.

        No `--resume`, which is the point. Everything else here writes *into* a
        conversation somebody is having, and two overlapping resumes of one
        session fork it silently — so work that is *about* a session, rather
        than part of it, has to happen somewhere else entirely. This is that
        somewhere else: a throwaway turn that reads what it is given and answers.

        Returns None on every failure. The caller is producing a convenience —
        a record of what a session knew before it was compacted — and a session
        must not be held up, or changed, because that could not be produced.
        """
        binary = self._binary
        if not binary or not text.strip():
            return None
        arguments = [binary, "-p"]
        if chosen := model or self._default_model:
            arguments += ["--model", chosen]
        arguments.append(text)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
        except OSError:
            logger.warning("Could not start the claude CLI for a one-shot turn", exc_info=True)
            return None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("A one-shot turn ran past %.0fs; giving up on it", timeout)
            return None
        if process.returncode != 0:
            reason = (
                (stderr or b"").decode("utf-8", "replace").strip()
                or (stdout or b"").decode("utf-8", "replace").strip()
                or "no output"
            )[:300]
            logger.warning("A one-shot turn failed (exit %s): %s", process.returncode, reason)
            return None
        answer = (stdout or b"").decode("utf-8", "replace").strip()
        return answer or None

    def _environment(self) -> dict[str, str]:
        """The environment one delivery runs in.

        A configured token is put in as `CLAUDE_CODE_OAUTH_TOKEN`, which is what
        `claude setup-token` mints and what a non-interactive run reads. It is
        set rather than defaulted: the whole point is to stop these turns
        depending on the desktop login, so a stale inherited value must not win.

        `ANTHROPIC_API_KEY` is deliberately left alone but *noticed*. It ranks
        above the token, and it bills per request against the API rather than
        against a subscription — so an inherited one silently changes both which
        credential is used and who pays. Saying so once is the difference
        between a surprise and a decision.
        """
        environment = os.environ.copy()
        if self._oauth_token:
            environment["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
            if environment.get("ANTHROPIC_API_KEY"):
                logger.warning(
                    "ANTHROPIC_API_KEY is set and outranks HALYARD_CLAUDE_OAUTH_TOKEN, so "
                    "these turns authenticate with the API key and bill against the API "
                    "rather than the subscription. Unset it to use the token."
                )
        return environment

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
                env=self._environment(),
            )
        except OSError:
            logger.exception("Could not start the claude CLI")
            return False

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            logger.error("A turn in %s ran past %.0fs; giving up on it", session_id, self._timeout)
            process.kill()
            await process.wait()
            return False

        if process.returncode != 0:
            # Both streams. The CLI says "Not logged in · Please run /login"
            # on *stdout*, and reading only stderr logged `failed (exit 1):`
            # with nothing after the colon — a delivery that failed for a
            # reason the machine had printed and this threw away.
            reason = (
                (stderr or b"").decode("utf-8", "replace").strip()
                or (stdout or b"").decode("utf-8", "replace").strip()
                or "no output"
            )[:400]
            self._last_error[session_id] = reason
            logger.error(
                "Delivering a message to %s failed (exit %s): %s",
                session_id,
                process.returncode,
                reason,
            )
            return False

        # The reply is not read from here. It arrives the same way every other
        # turn's does — through the Stop hook and the relay — so a message sent
        # from a phone and one typed at the desk come back by one path.
        return True
