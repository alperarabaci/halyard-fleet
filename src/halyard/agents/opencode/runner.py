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

from halyard.agents.base import SessionRef

logger = logging.getLogger(__name__)

#: Long enough for a slow first token, short enough that a phone is told
#: something. A turn itself runs far longer than this — the POST returns when
#: the message is accepted, not when the work is done.
TIMEOUT = 30.0


class OpencodeRunner:
    """Delivers into an opencode session over its own local API."""

    def __init__(self) -> None:
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
        """The model this runner would send with, and no effort setting.

        Effort is a Claude Code and Codex idea. opencode's message carries a
        model and nothing about how hard it thinks, so the honest answer is
        None rather than a level that would be dropped on the way.
        """
        return self._models.get(session_id), None

    def set_model(self, session_id: str, model: str | None) -> None:
        if model:
            self._models[session_id] = model
        else:
            self._models.pop(session_id, None)

    def set_effort(self, session_id: str, effort: str | None) -> None:
        """Accepted and ignored, because there is nowhere to put it."""
        return None

    # --- finding and sending ------------------------------------------------

    def resolve(self, name: str) -> SessionRef | None:
        from halyard.agents import opencode

        return opencode.find_session(name)

    def busy(self, session_id: str) -> bool:
        return session_id in self._busy

    async def send(self, session_id: str, text: str, cwd: str | None = None) -> bool:
        """Put `text` into the session as if it had been typed there.

        Never raises. The caller is handling a chat message, and a delivery
        that failed is worth reporting to the person waiting rather than
        propagating into the poll loop that has to read their next one.
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

        where = f"http://127.0.0.1:{opencode._port()}/session/{session_id}/message"
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
