"""Putting a message into a running opencode session.

The other three runners start a process. This one does not: opencode is already
running, holding the session somebody is working in, and it serves an HTTP API
for exactly this. So a turn from a phone is a POST, and it lands in the same
session that is open on the screen — which is the whole point, and the thing
that made this runtime worth adding.

**The model is chosen per message, not per session.** There is no "set the
session's model" anywhere in the API; every message carries its own. That is
not a limitation to work around, it is the shape: `set_model` here records what
*this* runner will send with, and the desk goes on choosing for itself. A turn
you start at the keyboard is unaffected, which is honest — Halyard has no way
to reach into the interface you are typing in.

The *variant* — opencode's name for the axis reasoning is lowered on — is one
of those things the interface keeps. Measured in 1.18.29: the binary carries
`command.model.variant.cycle` as a keybinding, and the message body has no
field for it. So it is chosen at the desk and left alone here. By 1.18.30 the
message body carries a `variant`, and `ask` — a turn of Halyard's own, in a
session nobody is looking at — sends one.

Nothing here starts opencode. If it is not running there is no session to write
into, and starting one would produce a second instance holding a different
conversation from the one somebody meant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from halyard.agents.base import SessionRef
from halyard.agents.turns import LateFailure, say_started
from halyard.core import usage

logger = logging.getLogger(__name__)

#: Where a message goes in without waiting for the answer.
#:
#: `/session/{id}/message` is the obvious one and it is the wrong one: it runs
#: the turn and answers when the turn is done. Measured — a message sent from a
#: phone reached the session, opencode began working, and thirty seconds later
#: this reported "that did not reach" while the reply was appearing on the
#: screen. The worst kind of wrong answer: the thing happened and the person
#: was told it had not.
#:
#: Nothing here wants the reply anyway. It arrives the way every other reply
#: does — through the plugin, as an event — and a runner that waited for it
#: would be holding a chat message open for the length of a turn.
PROMPT = "prompt_async"

#: Long enough for a busy server to accept a message, and no longer. What is
#: being waited for now is acceptance, not work.
TIMEOUT = 30.0

#: How a session Halyard opens for a turn of its own is titled: its own, in
#: any list it turns up in — the desk's among them, should it outlive its turn.
OWN_TITLE = "halyard: "

#: What a turn that edits nothing may not do, on top of the project's own
#: rules — which still decide what it has to ask about. Measured on 1.18.30:
#: a session's rules come after the project's, and the last one that matches
#: decides, so `bash: ask` stays and the command still reaches Halyard.
READING_ONLY = (
    {"permission": "edit", "pattern": "*", "action": "deny"},
    {"permission": "webfetch", "pattern": "*", "action": "deny"},
)


class OpencodeRunner:
    """Delivers into an opencode session over its own local API."""

    def __init__(self, usage_path: Path | None = None) -> None:
        #: Where what a turn of Halyard's own used is recorded, as the other
        #: runners record theirs. None records nothing.
        self._usage_path = usage_path
        #: What this runner sends with, per session. Empty means whatever the
        #: session is already using, which is the right default: somebody who
        #: has not chosen from the phone has chosen at the desk.
        self._models: dict[str, str] = {}
        #: Sessions this runner is mid-turn in. Only its own — a turn started
        #: at the keyboard is invisible from here, and saying otherwise would
        #: be worse than saying nothing.
        self._busy: set[str] = set()

    @property
    def id(self) -> str:
        return "opencode"

    # --- what can be chosen -------------------------------------------------

    def options(self, session_id: str | None = None) -> dict[str, tuple[tuple[str, ...], bool]]:
        """The models this machine's configuration offers, if it names any.

        Not enforced. A list written in a configuration file months ago has no
        business refusing a model that shipped this morning, and opencode
        passes an unknown one to its provider and lets that provider answer.
        """
        from halyard.core.config_file import runtime_settings

        try:
            configured = runtime_settings().get("opencode")
        except Exception:
            configured = None
        models = configured.models if configured else ()
        return {"model": (models, False)} if models else {}

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        """The model this runner would send with, and nothing about effort.

        Not because opencode has no such idea — it has one, called a *variant*,
        and somebody using it lowers reasoning that way. It is a choice made in
        the interface: measured in 1.18.29, the binary carries
        `command.model.variant.cycle` as a keybinding and the message body this
        runner posts to has no field for it. So a variant is set at the desk
        and stays set, and reporting one from here would be reporting a setting
        this code cannot reach.
        """
        return self._models.get(session_id), None

    def set_model(self, session_id: str, model: str | None) -> None:
        if model:
            self._models[session_id] = model
        else:
            self._models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
        """Accepted and dropped, because the message has nowhere to carry it.

        The channel refuses `/effort` for this runtime before reaching here —
        `options()` does not offer it, and a confirmation for something that
        did not happen is worse than a refusal. This stays because the protocol
        has it, and silently doing nothing is the honest implementation of a
        setting that cannot be sent.
        """
        return None

    # --- finding and sending ------------------------------------------------

    def resolve(self, name: str) -> SessionRef | None:
        from halyard.agents import opencode

        return opencode.find_session(name)

    def busy(self, session_id: str) -> bool:
        return session_id in self._busy

    async def send(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        when_done: LateFailure | None = None,
    ) -> bool:
        """Put `text` into the session as if it had been typed there.

        Never raises. The caller is handling a chat message, and a delivery
        that failed is worth reporting to the person waiting rather than
        propagating into the poll loop that has to read their next one.

        `when_done` is accepted and never called. What happens after `PROMPT`
        answers is not visible from here — no process to wait on, no exit code
        — and the runtime reports its own trouble through the plugin, which is
        the path a 429 already takes. Calling this on a guess would be worse
        than leaving it to the half that can see.
        """
        if not session_id or not (text or "").strip():
            return False

        from halyard.agents import opencode

        body: dict = {"parts": [{"type": "text", "text": text}]}
        if chosen := self._models.get(session_id):
            # `provider/model`, which is how opencode names one everywhere —
            # `zai-coding-plan/glm-5.3-flash`. Split once from the left: a model
            # id may carry slashes of its own.
            provider, _, model = chosen.partition("/")
            if model:
                body["model"] = {"providerID": provider, "modelID": model}

        where = f"http://127.0.0.1:{opencode._port()}/session/{session_id}/{PROMPT}"
        if cwd:
            from urllib.parse import quote

            where += f"?directory={quote(cwd, safe='')}"

        self._busy.add(session_id)
        try:
            return await asyncio.to_thread(self._post, where, body)
        finally:
            self._busy.discard(session_id)

    @staticmethod
    def _post(where: str, body: dict) -> bool:
        request = urllib.request.Request(
            where,
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answered:
                return 200 <= answered.status < 300
        except urllib.error.HTTPError as refused:
            # opencode's own words, which are more use than anything invented
            # here — a model it does not have, a session that has been deleted.
            said = ""
            try:
                said = refused.read().decode("utf-8", "replace")[:200]
            except Exception:
                said = ""
            logger.warning("opencode refused a message: %s %s", refused.code, said)
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as unreachable:
            logger.warning("Could not reach opencode: %s", unreachable)
            return False

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
        """One turn in a session of its own, in the opencode already running here.

        Not `opencode run`. Measured on 1.18.32: run without a terminal, it
        rejects every question it would have asked — "auto-rejecting" — before
        Halyard hears of it, so a turn whose one command needs allowing cannot
        have it. The opencode already running asks as it does for any session:
        through the plugin, to Halyard, a card when the rules say so.

        So this opens a session titled `halyard: <purpose>` — unable to edit a
        file or fetch a page when `edits` is False — sends one message with the
        model and its `variant`, which is what opencode calls effort (`low`,
        `high`, `max` for GLM 5.3), waits for the turn, and deletes the session.
        What every step of the turn used is added up and recorded under
        `session_id`, the caller's own id for the turn, so it joins whatever the
        caller keeps; output counts the model's reasoning as well.

        `started` is told the session's own id before the message goes: its
        questions and its reply arrive under that id, not the caller's.

        Returns the text of the last reply, or None on any failure.
        """
        if not (text or "").strip():
            return None
        from halyard.agents import opencode

        base = f"http://127.0.0.1:{opencode._port()}"
        where = f"?directory={quote(str(cwd), safe='')}" if cwd else ""
        opened = await asyncio.to_thread(
            self._call,
            "POST",
            f"{base}/session{where}",
            {
                "title": OWN_TITLE + (purpose or "a turn of its own"),
                **({} if edits else {"permission": list(READING_ONLY)}),
            },
            TIMEOUT,
        )
        ident = (opened or {}).get("id")
        if not ident:
            return None
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if model:
            provider, _, name = model.partition("/")
            if name:
                body["model"] = {"providerID": provider, "modelID": name}
        if effort:
            body["variant"] = effort
        if system:
            body["system"] = system
        try:
            await say_started(started, ident)
            answered = await asyncio.to_thread(
                self._call, "POST", f"{base}/session/{ident}/message{where}", body, timeout
            )
            steps = await asyncio.to_thread(
                self._call, "GET", f"{base}/session/{ident}/message{where}", None, TIMEOUT
            )
        finally:
            # Ended where it runs, then gone. Stopped by somebody or past its
            # time, a turn left going would carry on for nobody; finished, the
            # abort is a no-op. Shielded, because this runs while a stop is
            # still unwinding.
            await asyncio.shield(
                asyncio.to_thread(
                    self._call, "POST", f"{base}/session/{ident}/abort{where}", {}, TIMEOUT
                )
            )
            await asyncio.shield(
                asyncio.to_thread(
                    self._call, "DELETE", f"{base}/session/{ident}{where}", None, TIMEOUT
                )
            )
        info = (answered or {}).get("info") or {}
        if info.get("error"):
            logger.warning("opencode's turn failed: %s", json.dumps(info["error"])[:300])
        self._record(
            steps if isinstance(steps, list) else [],
            session_id=session_id or ident,
            model=model,
            purpose=purpose,
            project=project,
        )
        said = "\n".join(
            part.get("text", "")
            for part in (answered or {}).get("parts") or []
            if part.get("type") == "text"
        ).strip()
        return said or None

    def _record(
        self,
        steps: list,
        *,
        session_id: str,
        model: str | None,
        purpose: str | None,
        project: str | None,
    ) -> None:
        """What every step of a turn used, as one row. Never raises."""
        if self._usage_path is None:
            return
        read = wrote = cache_read = cache_write = 0
        cost = 0.0
        answered_by = model
        for step in steps:
            info = (step.get("info") or {}) if isinstance(step, dict) else {}
            if info.get("role") != "assistant":
                continue
            used = info.get("tokens") or {}
            cache = used.get("cache") or {}
            read += int(used.get("input") or 0)
            wrote += int(used.get("output") or 0) + int(used.get("reasoning") or 0)
            cache_read += int(cache.get("read") or 0)
            cache_write += int(cache.get("write") or 0)
            cost += float(info.get("cost") or 0.0)
            if info.get("providerID") and info.get("modelID"):
                answered_by = f"{info['providerID']}/{info['modelID']}"
        if not (read or wrote or cache_read or cache_write):
            return
        usage.record(
            self._usage_path,
            [
                usage.Turn(
                    self.id,
                    session_id,
                    answered_by,
                    purpose,
                    project,
                    read,
                    wrote,
                    cache_write,
                    cache_read,
                    cost,
                )
            ],
        )

    @staticmethod
    def _call(method: str, where: str, body: dict | None, timeout: float):
        """One request to opencode's API, and its answer as JSON — or None when
        it refused or could not be reached, said in the log in its own words."""
        request = urllib.request.Request(
            where,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"content-type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answered:
                raw = answered.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as refused:
            said = ""
            try:
                said = refused.read().decode("utf-8", "replace")[:200]
            except Exception:
                said = ""
            logger.warning("opencode refused %s %s: %s %s", method, where, refused.code, said)
            return None
        except (urllib.error.URLError, OSError, TimeoutError) as unreachable:
            logger.warning("Could not reach opencode for %s %s: %s", method, where, unreachable)
            return None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
