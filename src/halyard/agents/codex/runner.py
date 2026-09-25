"""Sending a message into a running Codex session.

`codex exec resume <id-or-name> "<text>"` continues a persisted session. Both
forms of address work and a UUID wins when the argument parses as one, which is
the CLI's own rule rather than a choice made here.

Measured against CLI `0.145.0`. Two things differ from Claude Code and both
shape this file:

**Effort depends on the model.** `ultra` exists on `gpt-5.6-sol` and
`gpt-5.6-terra` and on nothing else; `max` is absent from `gpt-5.5` and older.
A single list of effort levels would therefore be wrong for some session no
matter which list you picked, which is why `options()` takes a session.

**The catalog is a command, not a constant.** `codex debug models --bundled`
prints what this CLI knows about, so the list follows the installed CLI instead
of following this file. It is read once and kept: it costs a subprocess, and a
chat command should not pay that every time somebody types `/options`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from halyard.agents.codex.sessions import find_session
from halyard.agents.turns import (
    WEDGED_AFTER_SECONDS,
    LateFailure,
    Turns,
    end_group,
    say_started,
)
from halyard.core import usage

logger = logging.getLogger(__name__)

#: Same as the Claude Code runner, and for the same reason: this is the point
#: at which a turn is wedged rather than long. See `agents/turns.py`.
DEFAULT_WEDGED_AFTER_SECONDS = WEDGED_AFTER_SECONDS

#: What to offer when the catalog cannot be read — the CLI is missing, or a
#: future release renames the subcommand. Measured on 0.145.0, and deliberately
#: a fallback rather than the source of truth.
FALLBACK_MODELS: dict[str, tuple[str, ...]] = {
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
    "gpt-5.4": ("low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("low", "medium", "high", "xhigh"),
    "gpt-5.2": ("low", "medium", "high", "xhigh"),
}

#: The measured factory model. Unlike Claude Code, whose headless default is its
#: cheapest model, Codex's default is its current frontier one — so there is no
#: quiet downgrade to correct here, and nothing is forced.
DEFAULT_MODEL: str | None = None

#: What a one-shot turn that edits nothing runs under: a sandbox that reads and
#: never writes, and nothing that asks to be let out of it — a command it
#: refuses fails rather than becoming a question. Every command meets the
#: project's gate first, because `PreToolUse` fires before the sandbox is asked.
READING_ONLY = ("-s", "read-only", "-c", 'approval_policy="never"')

#: How long one line of `codex exec --json` may be. A command's whole output is
#: on the line that reports it, and a diff is easily more than the 64 KiB a
#: stream reader allows by default — the size of line that stopped the ZCode
#: bridge reading.
LINE_LIMIT = 32 * 1024 * 1024

_FALLBACK_BINARIES = (
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
    Path.home() / ".local" / "bin" / "codex",
)


#: How the CLI says a thread is open somewhere else.
#:
#: Codex 0.153 made a thread single-writer, where 0.145 let two resumes overlap.
#: The writer is normally the ChatGPT app's `app-server`, which holds the lock
#: for as long as the thread is open there — measured with `lsof` on both
#: machines, on a thread that was sitting idle. So the arrangement this project
#: is built for, a session open at the desk and driven from a phone, is exactly
#: the arrangement `exec resume` can no longer reach.
#:
#: Matched on the phrase rather than the exit code, because every other failure
#: exits the same way and must not be retried down the other path: queueing a
#: message for a session that is not logged in would report a delivery that
#: never happens.
_ANOTHER_WRITER = "already has an active writer"


def held_by_another(reason: str | None) -> bool:
    """Whether this failure was the thread being open somewhere else."""
    return _ANOTHER_WRITER in (reason or "")


def read_catalog(binary: str | None) -> dict[str, tuple[str, ...]] | None:
    """What *this* CLI knows, as model → the efforts it accepts. None if unasked.

    Bundled with the binary, so it is version-bound and authoritative about the
    one thing a list written here can never be: whether the installed CLI can
    run a given model. Measured across an upgrade — 0.145.0 answered with the
    `gpt-5.6` family and no `gpt-6-astra`, and 0.153.4 answered with it.

    **None and empty are different answers.** None means the question could not
    be asked, and a caller must not conclude anything from it: the built-in
    fallback list is months old by construction, and refusing a model because
    it is absent from *that* would be the confidently-wrong failure this
    project keeps having to undo.
    """
    if not binary:
        return None
    try:
        done = subprocess.run(
            [binary, "debug", "models", "--bundled"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        models = json.loads(done.stdout)["models"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        logger.warning("Could not read the Codex model catalog; using the built-in list")
        return None
    found = {}
    for model in models:
        slug = model.get("slug")
        efforts = tuple(
            level["effort"]
            for level in model.get("supported_reasoning_levels") or []
            if level.get("effort")
        )
        if slug and efforts:
            found[str(slug)] = efforts
    return found or None


def find_codex_binary(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    found = shutil.which("codex")
    if found:
        return found
    for candidate in _FALLBACK_BINARIES:
        if candidate.exists():
            return str(candidate)
    return None


class CodexRunner:
    """Delivers a message into a Codex session by resuming it."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        wedged_after_seconds: float = DEFAULT_WEDGED_AFTER_SECONDS,
        default_model: str | None = DEFAULT_MODEL,
        usage_path: Path | None = None,
    ) -> None:
        #: Where what a turn of Halyard's own used is recorded, as the other
        #: runners record theirs. None records nothing.
        self._usage_path = usage_path
        # The path is *not* resolved here. A control plane runs for days, and
        # what it can reach changes underneath it: a CLI installed after
        # startup stayed invisible until a restart, and Claude Code's binary
        # lives under a version number, so an upgrade moves it and leaves a
        # long-running process pointing at a path that no longer exists.
        # Measured: `doctor` found codex and the runner did not, in the same
        # minute, because one had asked at startup and the other just now.
        self._configured = binary
        self._turns = Turns("codex", wedged_after=wedged_after_seconds)
        self._default_model = default_model or None
        self._models: dict[str, str] = {}
        self._efforts: dict[str, str] = {}
        self._catalog: dict[str, tuple[str, ...]] | None = None

    @property
    def id(self) -> str:
        return "codex"

    @property
    def _binary(self) -> str | None:
        """Where the CLI is, asked now rather than remembered."""
        return find_codex_binary(self._configured)

    @property
    def available(self) -> bool:
        return self._binary is not None

    # --- what can be chosen ---------------------------------------------------

    def catalog(self) -> dict[str, tuple[str, ...]]:
        """Model → the effort levels that model accepts, read from the CLI once."""
        if self._catalog is not None:
            return self._catalog
        self._catalog = read_catalog(self._binary) or dict(FALLBACK_MODELS)
        return self._catalog

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        """What can be chosen, narrowed to the model this session is on.

        Reporting every effort any model accepts would offer `ultra` for a
        session running `gpt-5.5`, which the CLI then refuses — an answer that
        is wrong in the one place somebody looks to avoid being wrong.

        Models are a hint, as everywhere else: the catalog lists what this CLI
        knows about, and a name it has not heard of is still passed through.
        Effort is enforced, because it is a closed set per model and a typo
        costs a whole turn to discover.
        """
        catalog = self.catalog()
        model = self._effective_model(session_id)
        efforts = catalog.get(model or "", ())
        if not efforts:
            # No session, or a model outside the catalog. Offer the union, since
            # refusing everything would be worse than offering one level the
            # model may reject with a clear message of its own.
            efforts = tuple(dict.fromkeys(level for row in catalog.values() for level in row))
        return {"model": (tuple(catalog), False), "effort": (efforts, True)}

    def _effective_model(self, session_id: str | None) -> str | None:
        """What this session will actually run on, chosen or otherwise."""
        if session_id and (chosen := self._models.get(session_id)):
            return chosen
        if self._default_model:
            return self._default_model
        if not session_id:
            return None
        ref = find_session(session_id)
        return ref.model if ref else None

    # --- the AgentRunner surface ---------------------------------------------

    def resolve(self, name: str):
        """Find a session by its thread name, or by its id."""
        return find_session(name)

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        return self._models.get(session_id) or self._default_model, self._efforts.get(session_id)

    def set_model(self, session_id: str, model: str | None) -> None:
        if model:
            self._models[session_id] = model
        else:
            self._models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
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
        return self._turns.last_error(session_id)

    def busy(self, session_id: str) -> bool:
        return self._turns.busy(session_id)

    async def send(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        when_done: LateFailure | None = None,
    ) -> bool:
        """Resume the session with `text` as the next thing the user said.

        The lock is kept even though a measured pair of overlapping resumes both
        survived. One trial is not a guarantee, the cost of serialising is a
        queue rather than a failure, and the equivalent on Claude Code forks
        silently — losing a turn with nothing raised anywhere. Being wrong in
        the safe direction here costs a wait.

        Returns on acceptance rather than on completion — see
        `agents/turns.py`. `when_done` is called if an accepted turn then fails,
        which is how running out of usage halfway through still gets reported.
        """
        if not self._binary:
            logger.error("Cannot deliver a message: the codex CLI was not found.")
            return False
        if not text.strip():
            return False

        # Fall back to the session's own directory rather than inheriting this
        # process's. Measured, and it is a gate question rather than a tidiness
        # one: `codex exec resume` finds a session from anywhere — unlike
        # Claude Code — but the *project hooks* are resolved from the working
        # directory of the CLI process. Resuming a session from the wrong
        # directory therefore runs it under a different project's gate, or
        # under none, while looking entirely normal. A resume run from this
        # repository fired this repository's Stop hook for a session belonging
        # to a directory in /tmp.
        #
        # Codex also refuses to start outside a trusted directory, so a wrong
        # cwd is as likely to fail loudly as to fail quietly. Neither is worth
        # risking when the session records where it belongs.
        #
        # And the damage outlasts the turn: a resumed session records the
        # directory it was resumed *in*, so one run from the wrong place moves
        # the session there permanently. Measured by doing it — a session
        # belonging to a directory in /tmp now reports this repository as its
        # own, because a single measurement turn was run from here.
        if not cwd:
            ref = await asyncio.to_thread(find_session, session_id)
            cwd = ref.cwd if ref else None
            if not cwd:
                logger.error(
                    "Refusing to resume %s: no working directory is recorded for it, and "
                    "resuming from somewhere else would apply that directory's hooks.",
                    session_id,
                )
                return False

        arguments = [self._binary, "exec", "resume"]
        if model := self._models.get(session_id) or self._default_model:
            arguments += ["--model", model]
        if effort := self._efforts.get(session_id):
            # Effort has no flag of its own; it is a config override, and the
            # value is parsed as TOML — hence the quotes inside the string.
            arguments += ["-c", f'model_reasoning_effort="{effort}"']
        arguments += [session_id, text]

        accepted = await self._turns.start(
            session_id,
            arguments,
            cwd=cwd,
            env=os.environ.copy(),
            when_done=when_done,
            # Not a failure: it is answered below. Logged as one, it made three
            # reviews that each got an answer read as three that never arrived.
            expected=held_by_another,
        )
        if accepted or not held_by_another(self._turns.last_error(session_id)):
            return accepted

        # The thread is open at the desk, so run nothing — hand the message to
        # whoever is holding it. `queue` goes in by `thread/queue/add` rather
        # than `thread/resume`, which is the path the writer lock is not on.
        #
        # Second rather than first, because the two are not equals. A turn we
        # run is a turn we chose the model for, can report the end of, and can
        # say is still going; a queued one is none of those — it runs inside the
        # application, under that session's own settings. So this is what to do
        # when the better way is refused, not what to do by default.
        #
        # And refused costs nothing: the conflict is decided before any model
        # call. Measured on the failure that prompted this — the message went in
        # at 20:05:46 and was refused at 20:05:47.
        logger.info("%s is open elsewhere; queueing the message for it instead", session_id)
        return await self._turns.start(
            session_id,
            self._queueing(session_id, text),
            cwd=cwd,
            env=os.environ.copy(),
        )

    def _queueing(self, session_id: str, text: str) -> list[str]:
        """The command that hands a message to a session somebody else is running.

        No effort override: that is a config value read when a turn starts, and
        this turn starts inside the application. Passing it would be writing
        down a preference that nothing applies.
        """
        arguments = [self._binary or "codex", "queue", "--thread", session_id]
        if model := self._models.get(session_id) or self._default_model:
            arguments += ["--model", model]
        return [*arguments, "--message", text]

    # --- a turn of Halyard's own ----------------------------------------------

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        edits: bool = True,
        session_id: str | None = None,
        purpose: str | None = None,
        project: str | None = None,
        system: str | None = None,
        effort: str | None = None,
        started: Callable[[str], object] | None = None,
    ) -> str | None:
        """One turn in a thread of its own — `codex exec` — and nothing kept.

        Run where the project is, because Codex reads a project's hooks, the
        gate among them, from the directory the CLI runs in. Measured: the
        `PreToolUse` hook reached Halyard for a command a one-shot turn ran in
        alpha-engine. `--ephemeral` keeps the thread off disk, so it is listed
        nowhere afterwards, and when `edits` is False the sandbox only reads.

        The model and its effort go as `send` passes them. `system`, which
        `codex exec` has no place for, goes in front of the text. Stdin is
        closed: with anything else there, the CLI reads it as more of the prompt.

        The answer comes as JSON lines. The first names the thread — told to
        `started` at once, before the model has asked to run anything — and the
        last says what the turn used, recorded under `session_id`, the caller's
        own id for the turn. Codex counts cached input inside `input_tokens`
        and reasoning inside `output_tokens`; the row keeps them the way the
        other runtimes' rows do.

        Returns the last thing the model said, or None on any failure.
        """
        binary = self._binary
        if not binary or not (text or "").strip():
            return None
        arguments = [binary, "exec", "--json", "--ephemeral"]
        arguments += (
            list(READING_ONLY)
            if not edits
            else ["-s", "workspace-write", "-c", 'approval_policy="never"']
        )
        chosen = model or self._default_model
        if chosen:
            arguments += ["-m", chosen]
        if effort:
            arguments += ["-c", f'model_reasoning_effort="{effort}"']
        if cwd is not None:
            arguments += ["-C", str(cwd)]
        arguments.append(f"{system}\n\n{text}" if system else text)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=os.environ.copy(),
                start_new_session=True,
                limit=LINE_LIMIT,
            )
        except OSError:
            logger.warning("Could not start the codex CLI for a one-shot turn", exc_info=True)
            return None
        # Drained alongside, or a CLI with plenty to say on stderr — an MCP
        # server that cannot sign in says it every turn — fills the pipe and
        # stops writing to stdout.
        said_aside = asyncio.ensure_future(process.stderr.read()) if process.stderr else None
        try:
            said, used, thread = await asyncio.wait_for(
                self._read(process, started), timeout=timeout
            )
            await process.wait()
        except TimeoutError:
            end_group(process)
            await process.wait()
            logger.warning("A codex one-shot turn ran past %.0fs; giving up on it", timeout)
            return None
        except asyncio.CancelledError:
            # Stopped by somebody. What the turn started is a group of its own
            # and would carry on for a turn nobody is waiting on.
            end_group(process)
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
            raise
        finally:
            if said_aside is not None:
                aside = b""
                with contextlib.suppress(Exception):
                    aside = await asyncio.wait_for(said_aside, timeout=5)
                if process.returncode not in (0, None) and aside:
                    logger.warning(
                        "A codex one-shot turn failed (exit %s): %s",
                        process.returncode,
                        aside.decode("utf-8", "replace").strip()[-300:],
                    )
        if used:
            self._record(
                used,
                session_id=session_id or thread or "",
                model=chosen,
                purpose=purpose,
                project=project,
            )
        return said

    async def _read(self, process, started) -> tuple[str | None, dict | None, str | None]:
        """The turn's events as they come: the thread it runs as, the last
        thing the model said, and what it used — or no answer when it failed."""
        said: str | None = None
        used: dict | None = None
        thread: str | None = None
        failed: str | None = None
        assert process.stdout is not None
        async for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "thread.started" and not thread:
                thread = event.get("thread_id")
                await say_started(started, thread)
            elif kind == "item.completed" and item.get("type") == "agent_message":
                said = item.get("text") or said
            elif kind == "turn.completed":
                used = event.get("usage") or {}
            elif kind == "turn.failed":
                failed = str((event.get("error") or {}).get("message") or "it failed")
        if failed:
            logger.warning("A codex one-shot turn failed: %s", failed[:300])
            return None, used, thread
        return (said.strip() or None) if said else None, used, thread

    def _record(
        self,
        used: dict,
        *,
        session_id: str,
        model: str | None,
        purpose: str | None,
        project: str | None,
    ) -> None:
        """What the turn used, as the other runtimes' rows say it. Never raises."""
        if self._usage_path is None:
            return
        cached = int(used.get("cached_input_tokens") or 0)
        usage.record(
            self._usage_path,
            [
                usage.Turn(
                    self.id,
                    session_id,
                    model,
                    purpose,
                    project,
                    max(int(used.get("input_tokens") or 0) - cached, 0),
                    int(used.get("output_tokens") or 0),
                    int(used.get("cache_write_input_tokens") or 0),
                    cached,
                    None,
                )
            ],
        )
