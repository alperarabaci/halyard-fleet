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
import json
import logging
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from halyard.agents.codex.sessions import find_session

logger = logging.getLogger(__name__)

#: Same generous ceiling as the Claude Code runner: a real turn runs tools, and
#: each of those may stop for an approval decided by a human on a phone.
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

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

_FALLBACK_BINARIES = (
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
    Path.home() / ".local" / "bin" / "codex",
)


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
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        default_model: str | None = DEFAULT_MODEL,
    ) -> None:
        self._binary = find_codex_binary(binary)
        self._timeout = timeout_seconds
        self._default_model = default_model or None
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._models: dict[str, str] = {}
        self._efforts: dict[str, str] = {}
        self._catalog: dict[str, tuple[str, ...]] | None = None

    @property
    def id(self) -> str:
        return "codex"

    @property
    def available(self) -> bool:
        return self._binary is not None

    # --- what can be chosen ---------------------------------------------------

    def catalog(self) -> dict[str, tuple[str, ...]]:
        """Model → the effort levels that model accepts, read from the CLI once."""
        if self._catalog is not None:
            return self._catalog
        self._catalog = self._read_catalog() or dict(FALLBACK_MODELS)
        return self._catalog

    def _read_catalog(self) -> dict[str, tuple[str, ...]] | None:
        if not self._binary:
            return None
        try:
            done = subprocess.run(
                [self._binary, "debug", "models", "--bundled"],
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

    def busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        """Resume the session with `text` as the next thing the user said.

        The lock is kept even though a measured pair of overlapping resumes both
        survived. One trial is not a guarantee, the cost of serialising is a
        queue rather than a failure, and the equivalent on Claude Code forks
        silently — losing a turn with nothing raised anywhere. Being wrong in
        the safe direction here costs a wait.
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

        async with self._locks[session_id]:
            return await self._run(session_id, text, cwd)

    async def _run(self, session_id: str, text: str, cwd: str | None) -> bool:
        arguments = [self._binary, "exec", "resume"]
        if model := self._models.get(session_id) or self._default_model:
            arguments += ["--model", model]
        if effort := self._efforts.get(session_id):
            # Effort has no flag of its own; it is a config override, and the
            # value is parsed as TOML — hence the quotes inside the string.
            arguments += ["-c", f'model_reasoning_effort="{effort}"']
        arguments += [session_id, text]

        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=os.environ.copy(),
            )
        except OSError:
            logger.exception("Could not start the codex CLI")
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
        return True
