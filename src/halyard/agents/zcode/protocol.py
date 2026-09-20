"""Driving ZCode's engine over the protocol its own application drives it with.

ZCode listens on no port, but the engine it ships runs as `app-server --stdio`
and speaks newline-delimited JSON in both directions. Whoever starts it is its
*host*: the engine asks the host for the things it will not decide by itself,
and the host's answers are the whole reason this exists.

Three questions arrive, and each one matters:

- `session/requestRuntimePreferences` — answered with `PREFERENCES` before a
  session will materialise at all.
- `interaction/requestProviderRuntimeHeaders` — the credential for the model
  call, as `{"headersApplied": true, "requestAuth": {"apiKey": …}}`. The
  desktop answers this from its login; Halyard answers it from the token in
  `halyard.yaml` and from nowhere else.
- `interaction/requestPermission` — every side-effect tool, once the session is
  in a mode that asks. This is Halyard's gate: the answer is a card on a phone.
  Measured: an answer of `deny` stops the tool and its reason reaches the model
  in its own words; a request left unanswered is repeated every few seconds and
  the turn never ends, so an answer — even a refusal — is owed every time.

Anything else the engine asks is answered `{}`: a host that stays silent on a
question it does not know is a host that hangs a turn.

Nothing here knows about chats, seats or cards. It is given a way to ask, and
what that means is the runner's business.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: What the engine wants to know about its host before a session opens. The
#: values are the desktop's own, measured: search and memory belong to the
#: application's UI, and a question nobody can answer resolves itself.
PREFERENCES = {
    "nativeSearchEnhancementsEnabled": False,
    "memoryEnabled": False,
    "askUserQuestionAutoResolutionEnabled": True,
    "modelContextBudgetStrategy": "preflight-v1",
}

#: The mode that asks before a change — "Ask before changes" in the app. Set
#: after the session is open, every time: a session whose stored mode is
#: already this one still does not enforce until it is set again. Measured.
ASKING = "build"

#: How long one call to the engine may take. It answers in milliseconds; this
#: bounds a machine where it does not.
CALL_SECONDS = 30.0

#: How long to wait for the engine to speak first before asking it anything.
#: It comes up in about a second; this is the machine having a bad morning.
LISTENING_SECONDS = 15.0


@dataclass(frozen=True)
class Permission:
    """One tool the engine wants an answer about."""

    tool: str
    #: What the tool was asked to do, as the engine passes it on.
    input: dict
    #: The engine's own reading of the risk — `low`, `medium`, `high`.
    risk: str
    #: The call it belongs to, so a second ask about the same one is the same
    #: question rather than a second card.
    request_id: str
    tool_call_id: str
    #: The session it is running in, which is how a card finds its seat.
    session_id: str = ""
    #: What the caller knows about that seat and nothing here does — where its
    #: code is, what the session is called.
    about: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    """What Halyard decided, in the engine's words."""

    decision: str
    reason: str = ""


#: Asked for every side-effect tool; answering is not optional — see the module
#: docstring. Slow on purpose: it is somebody reading a phone.
Asking = Callable[[Permission], Awaitable[Answer]]


class Bridge:
    """One engine process, driven as its application drives it.

    Started for a message and kept alive while the turn runs, because the
    permission questions arrive throughout it. Closing it ends the turn, so it
    is closed when the turn ends and not before.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None = None,
        token: str = "",
        asking: Asking | None = None,
        about: Mapping[str, str] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = {**os.environ, **(env or {})}
        self._token = token
        self._asking = asking
        self._about = dict(about or {})
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._answers: dict[int, asyncio.Future] = {}
        self._asked: dict[str, asyncio.Task] = {}
        self._next = 0
        #: What the turn ended as: None while it runs, then the reply, or the
        #: failure the engine reported.
        self.reply: str | None = None
        self.failure: str | None = None
        self._ended = asyncio.Event()
        #: Set when the engine has spoken first — which is how it says it is
        #: listening. A call written before that is written into a process that
        #: is still starting, and is never answered.
        self._listening = asyncio.Event()
        #: Set when its output has closed, which is what a process that will
        #: not run at all looks like from here.
        self._gone = asyncio.Event()

    async def start(self) -> None:
        """Start the engine and begin answering it."""
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
        )
        self._reader = asyncio.create_task(self._read(), name="zcode-bridge")
        self._stderr = asyncio.create_task(self._complaints(), name="zcode-bridge-stderr")

    async def listening(self, timeout: float = LISTENING_SECONDS) -> bool:
        """Wait until the engine has said something of its own.

        It asks for the host's preferences as it comes up, and until it does it
        is not reading its input: a message written then is lost, and what that
        looks like from here is a call that is never answered.
        """
        try:
            await asyncio.wait_for(self._listening.wait(), timeout)
        except TimeoutError:
            logger.info("The ZCode engine said nothing in %.0fs; asking anyway", timeout)
            return False
        return not self._gone.is_set()

    async def call(self, method: str, params: dict, timeout: float = CALL_SECONDS) -> dict:
        """Ask the engine something and wait for its answer."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("the ZCode bridge is not running")
        if self._gone.is_set():
            raise RuntimeError("ZCode's engine stopped")
        self._next += 1
        where = self._next
        waiting: asyncio.Future = asyncio.get_running_loop().create_future()
        self._answers[where] = waiting
        self._write({"id": where, "method": method, "params": params})
        try:
            return await asyncio.wait_for(waiting, timeout)
        except TimeoutError:
            # Said with the method in it: an empty timeout in a log is a puzzle
            # somebody has to reproduce before they can read it.
            raise TimeoutError(f"{method} went unanswered for {timeout:.0f}s") from None
        finally:
            self._answers.pop(where, None)

    async def ended(self, timeout: float) -> bool:
        """Wait for the turn to end. False when it is still going."""
        try:
            await asyncio.wait_for(self._ended.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def close(self) -> None:
        """End the engine, and everything waiting on it."""
        for asked in list(self._asked.values()):
            asked.cancel()
        for watching in (self._reader, self._stderr):
            if watching is not None:
                watching.cancel()
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()

    # --- the conversation itself ------------------------------------------

    def _write(self, message: dict) -> None:
        if self._process is not None and self._process.stdin is not None:
            self._process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))

    async def _complaints(self) -> None:
        """Whatever the engine writes to its standard error.

        Read rather than ignored for two reasons: it is where it says why it
        will not do something, and a pipe nobody empties fills up and stops the
        process that is writing to it.
        """
        assert self._process is not None and self._process.stderr is not None
        while line := await self._process.stderr.readline():
            said = line.decode("utf-8", "replace").strip()
            if said:
                logger.info("ZCode's engine: %s", said[:400])

    async def _read(self) -> None:
        """Every line the engine says, until it stops saying anything."""
        assert self._process is not None and self._process.stdout is not None
        while line := await self._process.stdout.readline():
            self._listening.set()
            try:
                message = json.loads(line.decode("utf-8").strip() or "{}")
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") and message.get("id") is not None:
                self._asked_of_us(message)
            elif message.get("id") is not None:
                waiting = self._answers.get(message["id"])
                if waiting is not None and not waiting.done():
                    waiting.set_result(message)
            else:
                self._notified(message)
        self._stopped()

    def _stopped(self) -> None:
        """The engine has stopped talking, which is the end of everything.

        An engine that will not start — the wrong application, a machine that
        refuses it — says nothing and closes its output. Whoever is waiting for
        an answer is told now rather than after half a minute of a timeout that
        reads like a slow engine instead of a missing one. Counted as having
        spoken for the same reason: waiting out the rest of a timeout for a
        process that has closed its output helps nobody.
        """
        self._gone.set()
        self._ended.set()
        self._listening.set()
        for waiting in self._answers.values():
            if not waiting.done():
                waiting.set_exception(RuntimeError("ZCode's engine stopped"))

    def _asked_of_us(self, message: dict) -> None:
        """A question from the engine. Every one of them is answered."""
        method, where = message.get("method"), message.get("id")
        if method == "session/requestRuntimePreferences":
            self._write({"id": where, "result": PREFERENCES})
            return
        if method == "interaction/requestProviderRuntimeHeaders":
            # The one place the token is used, and it goes no further: the
            # engine signs and sends the model request itself.
            self._write(
                {
                    "id": where,
                    "result": {"headersApplied": True, "requestAuth": {"apiKey": self._token}},
                }
            )
            return
        if method == "interaction/requestPermission":
            self._permission(message)
            return
        self._write({"id": where, "result": {}})

    def _permission(self, message: dict) -> None:
        """Hand one tool to whoever answers for this seat, without stopping the
        conversation: the answer is a person, and the engine keeps talking."""
        params = message.get("params") or {}
        asked = Permission(
            tool=str(params.get("toolName") or ""),
            input=params.get("input") or params.get("toolInput") or {},
            risk=str(params.get("riskLevel") or ""),
            request_id=str(params.get("requestId") or ""),
            tool_call_id=str(params.get("toolCallId") or ""),
            session_id=str(params.get("sessionId") or ""),
            about=self._about,
        )
        if self._asking is None:
            self._write(
                {"id": message.get("id"), "result": {"decision": "deny", "reason": "no gate"}}
            )
            return
        if asked.request_id in self._asked:
            # The engine repeats an unanswered question every few seconds. One
            # card, asked once; this one waits for the same answer.
            return
        self._asked[asked.request_id] = asyncio.create_task(
            self._answer(message.get("id"), asked), name=f"zcode-permission-{asked.request_id}"
        )

    async def _answer(self, where, asked: Permission) -> None:
        try:
            answer = await self._asking(asked)  # type: ignore[misc]
        except Exception:
            logger.warning("Nothing answered for %s in ZCode; refusing", asked.tool, exc_info=True)
            answer = Answer(decision="deny", reason="Halyard could not be asked")
        self._write({"id": where, "result": {"decision": answer.decision, "reason": answer.reason}})
        logger.info("ZCode asked about %s: %s", asked.tool, answer.decision)

    def _notified(self, message: dict) -> None:
        params = message.get("params") or {}
        kind, payload = params.get("type"), params.get("payload") or {}
        if kind == "turn.completed":
            self.reply = str(payload.get("response") or "")
            self._ended.set()
        elif kind == "turn.failed":
            self.failure = str(
                payload.get("underlyingErrorMessage") or payload.get("message") or "it failed"
            )
            self._ended.set()
