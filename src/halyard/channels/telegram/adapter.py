"""The Telegram channel adapter.

Sends a card, listens for the button, hands the answer to the approval store.

It decides nothing. Whether a nonce is valid, whether a request is still open,
whether it has already been answered — all of that is the store's, and this file
calls into it and reacts to what comes back. The one judgement it does make is
*who is allowed to press the button*, because that is a fact about the channel
rather than about the approval.

Answers arrive by long polling. A webhook would need a public URL, and the whole
posture of this service is that it does not have one.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from halyard.agents.claude_code import find_session
from halyard.channels.telegram import cards
from halyard.channels.telegram.api import TelegramApi
from halyard.core.approvals import (
    AlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalRequest,
    ApprovalStore,
    Decision,
    InvalidNonceError,
    UnknownApprovalError,
)
from halyard.core.audit import (
    AuditLog,
    gate_changed,
    invalid_nonce,
    replayed_callback,
    unauthorized_callback,
    user_message,
)
from halyard.core.events import Role
from halyard.core.gate import Gate
from halyard.core.registry import SessionRegistry

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: How long a poll waits for something to happen before coming back empty.
POLL_TIMEOUT_SECONDS = 30

#: Backoff after a failed poll, so a Telegram outage does not become a tight
#: loop against their API. Doubles per consecutive failure up to the cap.
POLL_RETRY_SECONDS = 3.0

#: Ceiling on the backoff. A long outage should not turn into a long silence
#: after it ends, so recovery is never more than this far away.
POLL_RETRY_MAX_SECONDS = 30.0


def _default_clock() -> datetime:
    return datetime.now(UTC)


def parse_destination(value: str | None) -> tuple[str, int | None] | None:
    """Read `chat_id` or `chat_id:thread_id` into where a message goes.

    One syntax for both shapes a group can take: a chat of its own, or a forum
    topic inside a shared one. Which you want is a matter of how you like your
    phone organised, and not something the code should have an opinion about.
    """
    if not value:
        return None
    chat, _, thread = value.rpartition(":")
    if chat and thread.isdigit():
        return chat, int(thread)
    return value, None


class TelegramChannel:
    """Puts approvals in a chat and brings the answers back."""

    def __init__(
        self,
        *,
        api: TelegramApi,
        store: ApprovalStore,
        audit: AuditLog,
        chat_id: str,
        authorized_user_ids: frozenset[str],
        clock: Clock = _default_clock,
        poll_retry_seconds: float = POLL_RETRY_SECONDS,
        gate: Gate | None = None,
        project: str = "unknown",
        navigator_chat_id: str | None = None,
        driver_chat_id: str | None = None,
        registry: SessionRegistry | None = None,
        runner=None,
        session_names: dict[Role, str] | None = None,
    ) -> None:
        self._api = api
        self._gate = gate or Gate()
        self._project = project
        self._store = store
        self._audit = audit
        self._chat_id = chat_id
        # Two seats and a default. A role with nowhere of its own falls back to
        # the main chat, so an existing single-chat setup keeps working
        # untouched by any of this.
        self._routes = {
            Role.NAVIGATOR: parse_destination(navigator_chat_id),
            Role.DRIVER: parse_destination(driver_chat_id),
        }
        self._registry = registry
        self._runner = runner
        self._session_names = session_names or {}
        self._sending: set[asyncio.Task] = set()
        self._authorized = authorized_user_ids
        self._clock = clock
        self._poll_retry_seconds = poll_retry_seconds
        # The chat is remembered alongside the message, because a card that
        # was routed to a seat has to be edited in that seat. Keeping only
        # the message id means editing against the wrong chat, which fails
        # quietly and leaves live-looking buttons on a settled question.
        self._open: dict[str, tuple[ApprovalRequest, int, str]] = {}
        self._poller: asyncio.Task | None = None
        self._offset: int | None = None

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self) -> None:
        await self._api.open()
        self._poller = asyncio.create_task(self._poll_forever(), name="telegram-poll")

    async def stop(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            # Awaiting the cancelled task is what makes stop() actually wait for
            # the poll to unwind, rather than returning while it is still alive.
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None
        await self._api.close()

    # --- sending ------------------------------------------------------------

    def _route(self, role: Role | None) -> tuple[str, int | None]:
        """Where this role's traffic goes."""
        return (role and self._routes.get(role)) or (self._chat_id, None)

    async def send_approval_request(self, request: ApprovalRequest) -> str:
        """Put a card in the chat.

        Raising propagates to the service, which denies. That is the right
        outcome: an approval that never reached anybody is not an approval, and
        the alternative is a bridge blocked on a question nobody was asked.
        """
        self._forget_expired()
        text = cards.render(request, now=self._clock())
        markup = cards.keyboard(
            request, include_full=request.command_full != request.command_summary
        )
        chat_id, thread_id = self._route(request.role)
        message = await self._api.send_message(
            chat_id, text, reply_markup=markup, message_thread_id=thread_id
        )
        message_id = int(message["message_id"])
        self._open[cards.handle_of(request)] = (request, message_id, chat_id)
        return str(message_id)

    async def send_message(self, session_id: str, text: str, role: Role | None = None) -> str:
        """Send an agent's own words, split across messages if they are long.

        Escaped, because this is somebody else's prose: a reply mentioning a
        `<div>` is not markup, and sending it as markup makes Telegram refuse
        the whole message.
        """
        chat_id, thread_id = self._route(role)
        chunks = cards.split_for_telegram(text)
        message = None
        for index, chunk in enumerate(chunks, start=1):
            marker = f"<i>({index}/{len(chunks)})</i>\n" if len(chunks) > 1 else ""
            message = await self._api.send_message(
                chat_id, marker + html.escape(chunk), message_thread_id=thread_id
            )
        return str(message["message_id"]) if message else ""

    async def send_long_content(
        self, session_id: str, content: str, title: str, role: Role | None = None
    ) -> str:
        """Send something that will not fit in a message.

        As a file rather than a wall of split messages: a diff or a full command
        is something you want to be able to scroll and search, not reassemble
        from six chat bubbles.
        """
        chat_id, thread_id = self._route(role)
        if len(content) <= cards.MESSAGE_LIMIT - 200:
            message = await self._api.send_message(
                chat_id,
                f"<b>{html.escape(title)}</b>\n<pre>{html.escape(content)}</pre>",
                message_thread_id=thread_id,
            )
            return str(message["message_id"])
        filename = f"{title.lower().replace(' ', '-')}.txt"
        result = await self._api.send_document(
            chat_id, filename, content.encode("utf-8"), caption=title
        )
        return str(result["message_id"])

    # --- listening ----------------------------------------------------------

    async def _poll_forever(self) -> None:
        """Keep asking Telegram for updates until cancelled.

        Survives errors. If this loop stopped, no approval could ever be
        answered — they would all sit until their deadline and then be denied.
        That is the safe failure, but it is silent, so a poll that keeps failing
        says so at ERROR rather than letting the system look healthy.

        It says so *once*, though. The first failure logs a full traceback,
        because you need it to know what broke; every consecutive one after that
        logs a single line with a running count. A transient DNS failure that
        printed eighteen identical tracebacks is what prompted this — the noise
        made a recoverable blip look like a crash.
        """
        failures = 0
        while True:
            try:
                updates = await self._api.get_updates(
                    offset=self._offset, timeout=POLL_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures == 1:
                    logger.exception(
                        "Telegram poll failed; approvals cannot be answered until it recovers"
                    )
                else:
                    logger.error("Telegram poll still failing (%d consecutive): %s", failures, exc)
                await asyncio.sleep(self._backoff(failures))
                continue

            if failures:
                # Say so explicitly. Errors stopping is not something anyone
                # notices in a log; a line saying they stopped is.
                logger.warning("Telegram poll recovered after %d consecutive failures", failures)
                failures = 0

            for update in updates:
                self._offset = int(update["update_id"]) + 1
                try:
                    if callback := update.get("callback_query"):
                        await self._handle_callback(callback)
                    elif message := update.get("message"):
                        await self._handle_message(message)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad update must not take the loop down with it, or a
                    # single malformed message would silence every future answer.
                    logger.exception("Failed to handle a Telegram update")

    async def _handle_message(self, message: dict) -> None:
        """Handle a typed command.

        The same authorization as a button press, for the same reason: closing
        the gate stops anyone being asked, and a stranger must not be able to do
        that any more than they can approve something.

        Silent for anything that is not a command. A chat where the bot argues
        with every stray message is a chat nobody keeps notifications on for,
        and notifications are the entire point.
        """
        text = (message.get("text") or "").strip()
        if not text:
            return

        user_id = str((message.get("from") or {}).get("id", ""))
        if user_id not in self._authorized:
            await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
            logger.warning("Ignoring a message from unauthorized Telegram user %s", user_id)
            return

        actor = f"tg:{user_id}"
        here = str((message.get("chat") or {}).get("id") or "") or None
        thread = message.get("message_thread_id")

        if not text.startswith("/"):
            await self._forward_to_session(text, actor, here or "", thread)
            return

        command, _, argument = text.partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        argument = argument.strip()

        if command == "chat":
            # An explicit way to say the same thing as plain text. Worth having:
            # a bot in a group sees only commands while privacy mode is on, and
            # leaving that on is a reasonable thing to want.
            if not argument:
                await self._say("Usage: <code>/chat &lt;message&gt;</code>", here, thread)
                return
            await self._forward_to_session(argument, actor, here or "", thread)
            return
        if command == "pause":
            _, changed = await self._gate.pause(actor)
            if changed:
                await self._record(gate_changed(paused=True, actor=actor, project=self._project))
            await self._say(
                (
                    "⏸ <b>Paused.</b> Halyard steps out of the way — no approval "
                    "cards, no replies. Claude Code decides on its own again, "
                    "exactly as if the hook were not installed: whatever its "
                    "<code>permissions.allow</code> list covers runs without "
                    "asking anybody, and the rest it asks you at the desk."
                )
                if changed
                else "⏸ Already paused.",
                here,
                thread,
            )
        elif command == "resume":
            _, changed = await self._gate.resume(actor)
            if changed:
                await self._record(gate_changed(paused=False, actor=actor, project=self._project))
            await self._say(
                "▶️ <b>Resumed.</b> Approvals are coming back here."
                if changed
                else "▶️ Already running.",
                here,
                thread,
            )
        elif command in ("model", "effort"):
            await self._choose(command, argument, here, thread)
        elif command == "options":
            await self._say(self._options(), here, thread)
        elif command == "status":
            await self._say(await self._status(), here, thread)
        elif command in ("start", "help"):
            await self._say(
                "<b>Halyard</b>\n\n"
                "Type anything to send it into the session.\n\n"
                "/chat &lt;message&gt; — the same, said explicitly\n"
                "/model &lt;name&gt; — what answers, for turns sent from here\n"
                "/effort &lt;level&gt; — how hard it thinks\n"
                "/options — everything those two accept\n"
                "/status — what is happening right now\n"
                "/pause — step out of the way; Claude Code decides on its own\n"
                "/resume — start again",
                here,
                thread,
            )

    async def _forward_to_session(
        self, text: str, actor: str, chat_id: str, thread_id: int | None = None
    ) -> None:
        """Put a typed message into the session that chat belongs to.

        Started as a detached task rather than awaited. A turn runs tools, and
        each tool may stop for an approval — which arrives as a button press
        this same poll loop has to read. Waiting here for the turn to finish
        would mean waiting for an approval that can never be delivered.
        """
        if self._gate.paused:
            await self._say("⏸ Paused. Send /resume first.", chat_id, thread_id)
            return
        if self._runner is None or self._registry is None:
            await self._say(
                "This control plane cannot send messages into a session. That needs "
                "the claude CLI, so it has to run on the host rather than in a container.",
                chat_id,
                thread_id,
            )
            return

        found = await self._session_for(chat_id)
        if found is not None and self._runner.busy(found[0]):
            # The runner serialises per session, so this would sit in silence
            # until the turn before it finished. Silence is what makes people
            # think a message was lost.
            await self._say(
                "⏳ Still working on the last one — yours is queued behind it.",
                chat_id,
                thread_id,
            )
        if found is None:
            await self._say(
                "No session to send that to. Name the one you mean in .env — "
                "<code>HALYARD_NAVIGATOR_SESSION</code> or "
                "<code>HALYARD_DRIVER_SESSION</code> — using a name from "
                "<code>halyard sessions</code>.",
                chat_id,
                thread_id,
            )
            return

        task = asyncio.create_task(self._deliver(found, text, actor, chat_id, thread_id))
        # Held so the loop does not drop the only reference and cancel it.
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _session_for(self, chat_id: str) -> tuple[str, str, str | None] | None:
        """Which session a chat belongs to, as (session_id, project, directory).

        The configured name is tried first. It is addressable from a standing
        start — a control plane that restarted a second ago can still find the
        session — whereas the registry only knows what has fired a hook since it
        came up. Telling somebody to go run a command somewhere before they can
        send a message is not an answer.
        """
        role = self._role_for_chat(chat_id)

        if role is not None and (name := self._session_names.get(role)):
            found = await asyncio.to_thread(find_session, name)
            if found:
                return found.session_id, self._project, found.cwd
            logger.warning("No session found named %r for the %s seat", name, role.value)

        session = (
            await self._registry.latest_for_role(role)
            if role is not None
            else await self._registry.latest()
        )
        return (session.session_id, session.project, session.cwd) if session else None

    def _role_for_chat(self, chat_id: str) -> Role | None:
        for role, destination in self._routes.items():
            if destination and destination[0] == chat_id:
                return role
        return None

    async def _deliver(
        self,
        session: tuple[str, str, str | None],
        text: str,
        actor: str,
        chat_id: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        session_id, project, cwd = session
        delivered = False
        try:
            delivered = await self._runner.send(session_id, text, cwd)
        except Exception:
            logger.exception("Could not deliver a message to %s", session_id)
        finally:
            await self._record(
                user_message(
                    session_id=session_id,
                    actor=actor,
                    project=project,
                    length=len(text),
                    delivered=delivered,
                )
            )
        if not delivered:
            await self._say(
                "⚠️ That did not reach the session. Check the control plane's log.",
                chat_id,
                thread_id,
            )

    async def _status(self) -> str:
        state = await self._gate.state()
        open_requests = await self._store.list_open()
        lines = [
            f"<b>Halyard — {html.escape(self._project)}</b>",
            "",
            f"Gate: {'⏸ paused' if state.paused else '▶️ running'}",
        ]
        if state.changed_by:
            lines.append(f"  last changed by {html.escape(state.changed_by)}")

        seats = await self._describe_seats()
        if seats:
            lines += ["", "<b>Sessions</b>"]
            lines += seats

        lines.append("")
        lines.append(f"Open approvals: {len(open_requests)}")
        for request in open_requests[:5]:
            remaining = cards.format_remaining(request.expires_at, self._clock())
            lines.append(
                f"  • {html.escape(request.project)} — "
                f"<code>{html.escape(request.command_summary[:60])}</code> ({remaining})"
            )
        return "\n".join(lines)

    async def _choose(
        self, what: str, value: str, chat_id: str | None, thread_id: int | None
    ) -> None:
        """Show or set the model or effort a seat will use.

        Only for turns started from here. A turn begun at a keyboard uses
        whatever the app is set to, and nothing in this process can reach that —
        worth saying plainly rather than letting a setting look more powerful
        than it is.
        """
        if self._runner is None:
            await self._say("No runner: this control plane cannot start turns.", chat_id, thread_id)
            return

        found = await self._session_for(chat_id or "")
        if found is None:
            await self._say("No session for this chat.", chat_id, thread_id)
            return
        session_id, _, _ = found

        # Ask the runtime what it accepts rather than importing one runtime's
        # list. The channel held `EFFORT_LEVELS` from the Claude Code module
        # until a Codex investigation pointed at it: a chat layer that knows a
        # specific runtime's constants is the thing this architecture exists to
        # prevent, and it would have rejected a perfectly valid Codex effort.
        #
        # `enforced` is why the flag is in `options()` at all. Effort is a
        # closed set worth checking; models are not, and refusing one released
        # this morning because it is missing from a list written months ago
        # would be worse than passing it through.
        allowed, enforced = self._runner.options(session_id).get(what, ((), False))
        if value and enforced and value.lower() not in allowed:
            await self._say(
                f"{what.capitalize()} is one of: <code>{' '.join(allowed)}</code>",
                chat_id,
                thread_id,
            )
            return

        if value:
            setter = self._runner.set_model if what == "model" else self._runner.set_effort
            cleared = value.lower() in ("default", "clear", "reset")
            setter(session_id, None if cleared else value)
            if cleared:
                # Say what it fell back to rather than that it was cleared.
                # Clearing a model does not hand the choice to the session —
                # nothing here can reach a session's own setting — it hands it
                # to whatever this control plane was configured with, and the
                # difference is a model nobody chose answering for days.
                model, effort = self._runner.preferences(session_id)
                back_to = model if what == "model" else effort
                answer = (
                    f"Cleared. Turns from here will use <b>{html.escape(back_to)}</b>."
                    if back_to
                    else f"Cleared. Turns from here will not set a {what} at all."
                )
            else:
                answer = f"Turns started from here will use <b>{html.escape(value)}</b>."
            await self._say(answer, chat_id, thread_id)
            return

        model, effort = self._runner.preferences(session_id)
        chosen = model if what == "model" else effort
        ref = await asyncio.to_thread(
            find_session,
            self._session_names.get(self._role_for_chat(chat_id or "") or Role.NAVIGATOR, ""),
        )
        in_use = (ref.model if what == "model" else ref.effort) if ref else None
        lines = [f"<b>{what}</b>", f"  in the session: {html.escape(str(in_use or 'unknown'))}"]
        if chosen:
            lines.append(f"  from here: <b>{html.escape(chosen)}</b>")
        lines.append(f"\nSet with <code>/{what} &lt;value&gt;</code>, or <code>default</code>.")
        await self._say("\n".join(lines), chat_id, thread_id)

    def _options(self) -> str:
        """Everything that can be chosen, asked of the runtime rather than known.

        One message, because the question it answers — "what can I even say
        here?" — is asked from a phone, where reading a manual is not an option
        and a wrong guess costs a round trip.

        Nothing here is hardcoded in this module. A second runtime shows up in
        this output by existing, and a model list updated in the environment
        appears without a release.
        """
        if self._runner is None:
            return "No runner: this control plane cannot start turns."

        lines = [f"<b>{html.escape(self._runner.id)}</b>"]
        for name, (values, enforced) in self._runner.options().items():
            shown = " ".join(html.escape(v) for v in values)
            lines.append(f"\n/{name}  <code>{shown}</code>")
            if not enforced:
                # Otherwise a model released after this list was written looks
                # unavailable, and the honest answer is that it probably works.
                lines.append("  ↳ anything else is passed through and may work.")
        lines.append("\nAdd <code>default</code> to give a choice back to the session.")
        return "\n".join(lines)

    async def _describe_seats(self) -> list[str]:
        """One line per configured seat: what it is, and what is answering.

        Which model a seat is on is invisible from a phone otherwise, and in a
        navigator/driver pair the two are usually deliberately different — a
        thinking one and a cheap one. Worth being able to check before sending
        an expensive instruction to the wrong one.
        """
        lines: list[str] = []
        for role, name in self._session_names.items():
            ref = await asyncio.to_thread(find_session, name)
            label = f"{role.value}: <b>{html.escape(name)}</b>"
            if ref is None:
                lines.append(f"  {label} — not found")
                continue
            details = " · ".join(filter(None, [ref.model, ref.effort and f"effort {ref.effort}"]))
            busy = " · ⏳ working" if self._runner and self._runner.busy(ref.session_id) else ""
            lines.append(f"  {label}\n     at the desk: {html.escape(details) or 'unknown'}{busy}")
            if self._runner is not None:
                # Not the same thing as the line above, and the two are easy to
                # confuse into a wrong conclusion. What the app is set to says
                # nothing about what a message typed here will run on: a session
                # sitting on opus still answers a phone with whatever this
                # control plane sends, which is its own default until told.
                model, effort = self._runner.preferences(ref.session_id)
                mine = " · ".join(filter(None, [model, effort and f"effort {effort}"]))
                lines.append(f"     from here: {html.escape(mine) or 'the CLI default'}")
        return lines

    async def _say(
        self, text: str, chat_id: str | None = None, thread_id: int | None = None
    ) -> None:
        """Answer in the conversation that asked.

        Not in the configured default chat, which is where this used to go: ask
        the navigator group something and the reply appeared in a private chat
        with the bot, which reads as the message having been lost.
        """
        try:
            await self._api.send_message(
                chat_id or self._chat_id, text, message_thread_id=thread_id
            )
        except Exception:
            logger.warning("Could not answer a command", exc_info=True)

    async def _handle_callback(self, callback: dict) -> None:
        query_id = str(callback.get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))
        parsed = cards.parse_callback_data(callback.get("data") or "")

        if parsed is None:
            await self._dismiss(query_id)
            return

        handle, nonce, action = parsed
        entry = self._open.get(handle)
        request_id = entry[0].request_id if entry else None

        if user_id not in self._authorized:
            # Recorded, then ignored. No message back that would confirm the
            # request exists, or that this bot has anything to do with it.
            await self._record(
                unauthorized_callback(
                    actor=f"tg:{user_id}", request_id=request_id, channel="telegram"
                )
            )
            logger.warning("Ignoring callback from unauthorized Telegram user %s", user_id)
            await self._dismiss(query_id)
            return

        if entry is None:
            await self._dismiss(query_id, "That request is no longer open.")
            return

        request, message_id, chat_id = entry

        if action == cards.SHOW_FULL:
            await self.send_long_content(request.session_id, request.command_full, "Full command")
            await self._dismiss(query_id)
            return

        decision = Decision.ALLOW if action == cards.ALLOW else Decision.DENY
        actor = f"tg:{user_id}"

        try:
            await self._store.resolve(
                request.request_id, nonce=nonce, decision=decision, decided_by=actor
            )
        except AlreadyResolvedError:
            await self._record(replayed_callback(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id, "Already decided.")
            return
        except InvalidNonceError:
            await self._record(invalid_nonce(actor=actor, request_id=request.request_id))
            logger.warning("Callback for %s carried a bad nonce", request.request_id)
            await self._dismiss(query_id)
            return
        except ApprovalExpiredError:
            await self._settle_card(request, message_id, chat_id, "deny", None)
            await self._dismiss(query_id, "Too late — that expired and was denied.")
            return
        except UnknownApprovalError:
            # The store has evicted it, so nothing here can be resolved again.
            self._open.pop(handle, None)
            await self._dismiss(query_id, "That request is no longer open.")
            return

        await self._settle_card(request, message_id, chat_id, decision.value, actor)
        await self._dismiss(query_id, "Allowed." if decision is Decision.ALLOW else "Denied.")

    # --- helpers ------------------------------------------------------------

    def _backoff(self, failures: int) -> float:
        """Wait longer as failures pile up, but never longer than the cap."""
        return min(self._poll_retry_seconds * (2 ** (failures - 1)), POLL_RETRY_MAX_SECONDS)

    async def _settle_card(
        self,
        request: ApprovalRequest,
        message_id: int,
        chat_id: str,
        decision: str,
        by: str | None,
    ) -> None:
        """Rewrite the card to show the outcome and drop the buttons."""
        try:
            await self._api.edit_message_text(
                chat_id,
                message_id,
                cards.render_resolved(request, decision=decision, by=by),
                reply_markup=None,
            )
        except Exception:
            # Cosmetic. The decision is already recorded and the nonce is spent,
            # so a stale-looking card is untidy rather than dangerous.
            logger.warning("Could not update the card for %s", request.request_id, exc_info=True)

    async def _dismiss(self, query_id: str, text: str | None = None) -> None:
        if not query_id:
            return
        try:
            await self._api.answer_callback_query(query_id, text=text)
        except Exception:
            logger.debug("Could not answer callback query %s", query_id, exc_info=True)

    async def _record(self, record) -> None:
        try:
            await self._audit.record(record)
        except Exception:
            logger.exception("Could not record %s", record.action.value)

    def _forget_expired(self) -> None:
        """Drop cards that can no longer be acted on.

        Decided requests are deliberately *not* dropped here. Forgetting one the
        moment it is answered would make a second press look like a press on
        something unknown, and it would go unrecorded — but a button being
        pressed twice is exactly the kind of thing an audit log exists for. They
        are held until their deadline passes, by which point the store refuses
        them anyway.
        """
        now = self._clock()
        for handle in [h for h, (r, _, _) in self._open.items() if now >= r.expires_at]:
            del self._open[handle]
