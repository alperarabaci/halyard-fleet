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
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from halyard import commits
from halyard.agents.base import AgentRunner
from halyard.applications import catalogue, desktop
from halyard.channels.telegram import cards, commit_card
from halyard.channels.telegram.api import TelegramApi
from halyard.commands import catalogue as commands_offered
from halyard.commands import running as commands_running
from halyard.core import prompts as configured_prompts
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
from halyard.core.config_file import Project
from halyard.core.events import Role
from halyard.core.gate import Gate
from halyard.core.questions import (
    AlreadyAnsweredError,
    QuestionExpiredError,
    QuestionRequest,
    QuestionStore,
    UnknownQuestionError,
)
from halyard.core.questions import (
    InvalidNonceError as QuestionInvalidNonceError,
)
from halyard.core.registry import SessionRegistry
from halyard.core.seats import Seat, find, for_chat, for_session
from halyard.core.seats import _default_runtime as default_runtime

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


#: What this bot answers, in the order a person meets them.
#:
#: One list, because there were two: a `/help` message written by hand, and
#: nothing at all registered with Telegram. A command the client has never
#: heard of does not appear when you type `/`, so every one of these had to be
#: remembered and typed in full — on a phone, which is the only place this is
#: ever used.
#:
#: Telegram's own limits, worth knowing before adding one: the name is
#: lowercase letters, digits and underscores, at most 32 characters, and the
#: description at most 256. Anything else is rejected for the whole list.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "Send a message into this seat's session"),
    ("to", "Send a message to another seat by name"),
    ("commit", "Commit this branch's work, with a message to approve"),
    ("open", "Open an agent on the machine — claude, codex, gemini"),
    ("command", "Run one of this project's own commands"),
    ("status", "What is happening right now"),
    ("options", "Models and effort levels this seat accepts"),
    ("model", "Choose what answers, for turns sent from here"),
    ("effort", "Choose how hard it thinks"),
    ("pause", "Step aside — the runtime decides on its own"),
    ("resume", "Take the gate back"),
    ("help", "This list"),
)


#: How the prompt names the seat it is waiting for.
#:
#: Written into the message so that a reply to it carries the seat back. That
#: was meant to be the whole mechanism — nothing remembered between the button
#: and the sentence somebody types. It is not enough on its own: `force_reply`
#: only *asks* the client to attach the question, and when it does not, the
#: sentence arrives looking like an ordinary message and goes to the seat that
#: owns the chat. Measured twice, on a message meant for somebody else.
ASK_FOR_TEXT = "Send what to {seat}?"
_ASKED = re.compile(r"^Send what to (\S+)\?")

#: How long a picked seat waits for the sentence that goes with it.
#:
#: Long enough to type a paragraph, short enough that a tap abandoned before
#: lunch is not still holding the next thing said after it.
HANDOFF_SECONDS = 300

#: How the prompt names the commit it is waiting for a message for.
#:
#: The same shape as `ASK_FOR_TEXT`, and for the same reason: a reply carries
#: the question back, so the sentence somebody types is matched to the proposal
#: it belongs to rather than to whichever was most recent.
ASK_FOR_MESSAGE = "Send the commit message for {handle}?"
_ASKED_MESSAGE = re.compile(r"^Send the commit message for (\S+)\?")

#: The one-shot model that writes the subject line. Named here rather than in
#: `halyard.commits`, which is deliberately ignorant of runtimes: this is a
#: Claude Code alias, and the channel is what holds the runners.
MESSAGE_MODEL = "sonnet"

#: How long to wait for it. A commit message is one short line; anything slower
#: than this has gone wrong, and the phone should hear that rather than hold.
MESSAGE_TIMEOUT_SECONDS = 120.0


def _seat_being_asked_for(text: str) -> str | None:
    """The seat named by one of our own prompts, or None if this is not one."""
    found = _ASKED.match((text or "").strip())
    return found.group(1) if found else None


def _commit_being_asked_for(text: str) -> str | None:
    """The proposal named by one of our own prompts, or None."""
    found = _ASKED_MESSAGE.match((text or "").strip())
    return found.group(1) if found else None


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _SessionTarget:
    """A resolved session together with the runtime that owns it.

    Keeping these together matters. A session id is only meaningful to its
    runtime: handing a Codex id to Claude Code produces the very plausible
    "No conversation found" error that hid this boundary leak.
    """

    session_id: str
    project: str
    cwd: str | None
    runner: AgentRunner


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
        question_store: QuestionStore | None = None,
        authorized_user_ids: frozenset[str],
        clock: Clock = _default_clock,
        poll_retry_seconds: float = POLL_RETRY_SECONDS,
        gate: Gate | None = None,
        project: str = "unknown",
        navigator_chat_id: str | None = None,
        driver_chat_id: str | None = None,
        registry: SessionRegistry | None = None,
        runner=None,
        runners: dict[str, object] | None = None,
        seats: list[Seat] | None = None,
        session_names: dict[Role, str] | None = None,
        prompts: Mapping[str, str] | None = None,
        repositories: Mapping[str, Project] | None = None,
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
        # Four groups where there used to be two: a navigator and a driver for
        # each runtime. Which one a card belongs to is (role, runtime), and
        # both halves already travel with the request — the hook says what the
        # session is called and `agent_id` says what ran it. Nothing is chosen
        # at the moment of sending; the configuration decides, as it did
        # before, and there is simply more of it.
        self._seats = list(seats or [])
        # Each project as configured — where its code is, and what has to pass
        # before a commit is offered from it. Empty is a fine state: `/commit`
        # then says it does not know rather than guessing, and every other
        # command is unaffected.
        self._repositories = dict(repositories or {})
        self._registry = registry
        # One runner per runtime, shared by every seat that uses it.
        self._runners = dict(runners or {})
        self._runner = runner or next(iter(self._runners.values()), None)
        self._session_names = session_names or {}
        # Sentences somebody says often enough to want a name for. Configured,
        # so changing the wording of one is editing a file rather than waiting
        # for a release.
        self._prompts = dict(prompts if prompts is not None else configured_prompts.DEFAULTS)
        self._sending: set[asyncio.Task] = set()
        # Who pressed a seat button and has not yet said what to send. Keyed by
        # the person as well as the chat: in a group, somebody else typing must
        # not be swept into a hand-off they did not ask for.
        self._handoffs: dict[tuple[str, int | None, str], tuple[str, datetime]] = {}
        self._authorized = authorized_user_ids
        self._clock = clock
        #: Which command is running in which project, so a second one is
        #: refused rather than started on top of it.
        self._working: dict[str, str] = {}
        #: Commits proposed and not yet answered. Owned by `halyard.commits`,
        #: which is where taking-once and going-stale are decided.
        self._proposals = commits.Proposals(self._clock)
        self._poll_retry_seconds = poll_retry_seconds
        # The chat is remembered alongside the message, because a card that
        # was routed to a seat has to be edited in that seat. Keeping only
        # the message id means editing against the wrong chat, which fails
        # quietly and leaves live-looking buttons on a settled question.
        self._open: dict[str, tuple[ApprovalRequest, int, str]] = {}
        # Questions are held apart from approvals: a different store answers
        # them, and their button carries an option index rather than allow/deny.
        # Same shape otherwise — handle to (request, message id, chat).
        self._question_store = question_store
        self._open_questions: dict[str, tuple[QuestionRequest, int, str]] = {}
        self._poller: asyncio.Task | None = None
        self._offset: int | None = None
        # Which chat the command being handled came from, so a listing can mark
        # the seat you are already standing in.
        self._here: str = ""

    @property
    def name(self) -> str:
        return "telegram"

    def _menu(self) -> tuple[tuple[str, str], ...]:
        """Every command this bot answers: the built-in ones, then yours.

        One list, used both for the menu Telegram publishes and for `/help`.
        Two lists would drift, and the way you would find out is by typing a
        command the menu offered and being ignored.
        """
        return COMMANDS + tuple(
            (name, configured_prompts.describe(text)) for name, text in self._prompts.items()
        )

    async def start(self) -> None:
        await self._api.open()
        # Registered so they appear when somebody types `/`. Best-effort: the
        # bot answers every one of these whether Telegram knows about them or
        # not, and a control plane that would not start because a menu could
        # not be published would be trading the thing for the label on it.
        try:
            await self._api.set_my_commands(self._menu())
        except Exception:
            logger.warning("Could not register the command list with Telegram", exc_info=True)
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

    def _route(
        self,
        role: Role | None,
        session_name: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, int | None]:
        """Where this seat's traffic goes.

        By whatever the seat was configured with — a name or a session id.
        A role stopped being enough the moment a second runtime arrived: a
        Claude driver and a Codex driver are both `driver`, and their cards
        belong in different groups.

        Both are accepted because they fail differently. A name is readable and
        is what you would copy out of an app, and it can be changed there
        without anybody remembering that a seat pointed at it. An id is
        unreadable and permanent. Matching either means a configuration written
        with one is not quietly wrong once somebody uses the other.

        **Always with the runtime.** The address is `(runtime, session)`, and
        `for_session` is where that is enforced — matching a bare name sent
        Antigravity's reply into the Claude driver's group, because both seats
        are named `alpha-engine-driver` and the Claude one is listed first.

        Then by role and runtime, then by role alone, then the default chat —
        so a setup with two seats keeps behaving exactly as it did.
        """
        owner = for_session(self._seats, agent_id, session_name, session_id)
        if owner is not None and owner.chat:
            return parse_destination(owner.chat) or (self._chat_id, None)
        # Only when a role was actually declared. Falling back on `None`
        # matches any seat that also has no role, so a seat with nowhere of its
        # own to speak would borrow the group of an unrelated one — measured
        # with seven seats, where a scratch seat's card landed in a reviewer's
        # group. No destination should mean the default chat, not somebody
        # else's.
        if role is not None:
            for seat in self._seats:
                if (
                    seat.role is role
                    and seat.chat
                    and (agent_id is None or seat.runtime == agent_id)
                ):
                    return parse_destination(seat.chat) or (self._chat_id, None)
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
        chat_id, thread_id = self._route(
            request.role, request.session_name, request.agent_id, request.session_id
        )
        message = await self._api.send_message(
            chat_id, text, reply_markup=markup, message_thread_id=thread_id
        )
        message_id = int(message["message_id"])
        self._open[cards.handle_of(request)] = (request, message_id, chat_id)
        # Where a card went, said once per card. "It arrived in the wrong
        # group" is otherwise a question only the person holding the phone can
        # answer, and the answer decays as soon as they scroll.
        logger.info(
            "Card for %s (%s, session %r) sent to %s",
            request.project,
            request.agent_id,
            request.session_name or "unnamed",
            chat_id,
        )
        return str(message_id)

    async def send_question(self, request: QuestionRequest) -> str:
        """Put a question card in the seat's chat.

        Routed exactly like an approval — a question from a Codex driver and one
        from a Claude driver belong in different places for the same reason a
        card does. Raising propagates to the service, which falls back to the
        terminal picker; a question that reached nobody is not one to block on.
        """
        self._forget_expired_questions()
        text = cards.render_question(request, now=self._clock())
        markup = cards.question_keyboard(request)
        chat_id, thread_id = self._route(
            request.role, request.session_name, request.agent_id, request.session_id
        )
        message = await self._api.send_message(
            chat_id, text, reply_markup=markup, message_thread_id=thread_id
        )
        message_id = int(message["message_id"])
        self._open_questions[cards.question_handle_of(request)] = (request, message_id, chat_id)
        logger.info(
            "Question for %s (%s, session %r) sent to %s",
            request.project,
            request.agent_id,
            request.session_name or "unnamed",
            chat_id,
        )
        return str(message_id)

    async def send_message(
        self,
        session_id: str,
        text: str,
        role: Role | None = None,
        *,
        agent_id: str | None = None,
        session_name: str | None = None,
    ) -> str:
        """Send an agent's own words, split across messages if they are long.

        Escaped, because this is somebody else's prose: a reply mentioning a
        `<div>` is not markup, and sending it as markup makes Telegram refuse
        the whole message.
        """
        chat_id, thread_id = self._route(role, session_name, agent_id, session_id)
        chunks = cards.split_for_telegram(text)
        message = None
        for index, chunk in enumerate(chunks, start=1):
            marker = f"<i>({index}/{len(chunks)})</i>\n" if len(chunks) > 1 else ""
            message = await self._api.send_message(
                chat_id, marker + html.escape(chunk), message_thread_id=thread_id
            )
        return str(message["message_id"]) if message else ""

    async def send_long_content(
        self,
        session_id: str,
        content: str,
        title: str,
        role: Role | None = None,
        *,
        agent_id: str | None = None,
        session_name: str | None = None,
    ) -> str:
        """Send something that will not fit in a message.

        As a file rather than a wall of split messages: a diff or a full command
        is something you want to be able to scroll and search, not reassemble
        from six chat bubbles.
        """
        chat_id, thread_id = self._route(role, session_name, agent_id, session_id)
        if len(content) <= cards.MESSAGE_LIMIT - 200:
            message = await self._api.send_message(
                chat_id,
                f"<b>{html.escape(title)}</b>\n<pre>{html.escape(content)}</pre>",
                message_thread_id=thread_id,
            )
            return str(message["message_id"])
        filename = f"{title.lower().replace(' ', '-')}.txt"
        result = await self._api.send_document(
            chat_id,
            filename,
            content.encode("utf-8"),
            caption=title,
            message_thread_id=thread_id,
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
            # A question open in this chat takes the message as its answer — the
            # "Other" path. The session is blocked waiting on it, so this is what
            # the words are for; a normal forward could not run anyway. Checked
            # before anything else for that reason.
            if here and await self._answer_open_question_with_text(here, text, user_id):
                return

            replied = (message.get("reply_to_message") or {}).get("text") or ""
            # Two ways to know this sentence belongs to a seat that was picked
            # a moment ago, and either is enough. The reply carries the question
            # itself, which is exact; the pending hand-off is what remains when
            # the client did not attach it. Relying on the reply alone put two
            # messages in front of an agent nobody had chosen.
            # A reply to a commit prompt is that commit's message, not a
            # sentence for a session. Taken before the seat hand-off below,
            # which would otherwise claim it and send it to an agent.
            if (waiting := _commit_being_asked_for(replied)) is not None:
                await self._rewrite_commit(waiting, text, here or "", thread, user_id)
                return

            answering = _seat_being_asked_for(replied) or self._take_handoff(here, thread, user_id)
            # The line that was missing while this was being guessed at: where
            # the message went, and what it arrived attached to.
            logger.info(
                "Message from %s → %s (replying to %r)",
                actor,
                f"seat {answering}" if answering else "this chat's own seat",
                replied[:60],
            )
            if answering:
                await self._forward_to_seat(f"{answering} {text}", actor, here or "", thread)
                return
            await self._forward_to_session(text, actor, here or "", thread)
            return

        # Any command means the sentence never came. Dropping the hand-off here
        # keeps it from attaching itself to something typed much later.
        self._take_handoff(here, thread, user_id)

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
        if command == "to":
            # What is being handed over: the message this replies to, or the
            # rest of the line. Replying is the natural gesture for "this one,
            # over there", and it means the text never has to be retyped.
            reply_to = message.get("reply_to_message") or {}
            replied = (reply_to.get("text") or "").strip()
            # Whichever message holds the text is what the buttons hang off.
            anchor = reply_to.get("message_id") if replied else message.get("message_id")
            await self._forward_to_seat(
                argument, actor, here or "", thread, replied=replied, anchor_id=anchor
            )
            return
        if command == "commit":
            await self._propose_commit(here or "", thread)
            return
        if command == "open":
            await self._open_application(argument, here or "", thread)
            return
        if command == "command":
            await self._run_command(argument, here or "", thread)
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
            await self._say(self._options(here), here, thread)
        elif command == "status":
            await self._say(await self._status(), here, thread)
        elif command in self._prompts:
            # A sentence somebody says often enough to have named. Whatever
            # follows the command is added to it, so `/md the failing test`
            # arrives as the prompt with that on the end.
            said = self._prompts[command]
            await self._forward_to_session(
                f"{said}\n\n{argument}" if argument else said, actor, here or "", thread
            )
        elif command in ("start", "help"):
            listed = "\n".join(f"/{name} — {description}" for name, description in self._menu())
            await self._say(
                "<b>Halyard</b>\n\nType anything to send it into the session.\n\n" + listed,
                here,
                thread,
            )

    def _seat_list(self) -> str:
        """Every seat that can be addressed, as a person would pick one from.

        Printed whenever a label is missing or wrong, rather than kept in a
        separate `/seats` command — the moment you need the list is the moment
        you got the name wrong, and a second command to go and look it up is a
        second thing to remember.
        """
        if not self._seats:
            return "No seats are configured."
        lines = []
        for seat in self._seats:
            where = " (this chat)" if seat.chat and seat.chat.split(":")[0] == self._here else ""
            lines.append(
                f"  <code>{html.escape(seat.label)}</code> — "
                f"{html.escape(seat.runtime)}"
                f"{' · ' + html.escape(seat.session) if seat.session else ''}{where}"
            )
        return "Seats you can send to:\n" + "\n".join(lines)

    async def _forward_to_seat(
        self,
        argument: str,
        actor: str,
        chat_id: str,
        thread_id: int | None = None,
        replied: str = "",
        anchor_id: int | None = None,
    ) -> None:
        """Send a message to a seat by name, from anywhere.

        Eight groups is what a person ends up with — a navigator and a driver
        per runtime, per machine — and each one talks to exactly one session.
        Handing a question to a different agent meant finding its group and
        retyping the question in it.

        This is the second way to reach a seat, and the one `seats.py` was
        written for: *"any seat can be named explicitly from anywhere, which is
        what makes it possible to take what one seat just wrote and hand it to
        another."* The lookup has been there since; nothing called it.

        **The reply still goes to the seat's own chat, not this one.** That is
        the property worth keeping: a seat's conversation stays in one place,
        readable from top to bottom, rather than being split across whichever
        group somebody happened to be standing in.
        """
        self._here = chat_id
        label, _, typed = argument.partition(" ")
        label, typed = label.strip(), typed.strip()
        # What was typed wins over what was replied to: somebody who wrote out
        # a message meant that one, and silently sending the other instead
        # would be the worst kind of helpful.
        text = typed or replied

        if not text and not label:
            # Nothing to send and no seat named: ask which, and the press will
            # ask for the message. Two taps, no memory.
            await self._offer_seats("", chat_id, thread_id, anchor_id)
            return

        if not text:
            await self._ask_for_text(label, chat_id, thread_id, actor.removeprefix("tg:"))
            return

        if not label:
            # The text is known and the seat is not, which is the one case a
            # button can finish on its own — pressing it needs nothing
            # remembered, because the message it applies to is the one the
            # buttons are attached to.
            await self._offer_seats(text, chat_id, thread_id, anchor_id)
            return

        seat = find(self._seats, label)
        if seat is None:
            await self._say(
                f"No seat called <b>{html.escape(label)}</b>.\n\n" + self._seat_list(),
                chat_id,
                thread_id,
            )
            return

        destination, destination_thread = parse_destination(seat.chat) or (chat_id, thread_id)
        # Said in both places on purpose. The person sending it is not looking
        # at the seat they sent it to, and the seat's own chat should not
        # acquire a message from nowhere.
        await self._say(
            f"→ sent to <b>{html.escape(seat.label)}</b> ({html.escape(seat.runtime)})",
            chat_id,
            thread_id,
        )
        if destination != chat_id:
            await self._say(
                f"↪ from <b>{html.escape(actor)}</b>, via another chat:\n\n{html.escape(text)}",
                destination,
                destination_thread,
            )
        await self._forward_to_session(text, actor, destination, destination_thread)

    def _remember_handoff(self, seat: str, chat_id: str, thread_id: int | None, user: str) -> None:
        """Note that this person picked a seat and owes us a sentence."""
        self._handoffs[(chat_id, thread_id, user)] = (seat, self._clock())

    def _take_handoff(self, chat_id: str | None, thread_id: int | None, user: str) -> str | None:
        """The seat this person picked, if they picked one recently enough.

        Taken rather than read: a hand-off covers the next thing said and
        nothing after it. Leaving it in place would mean a tap made once
        quietly redirecting a conversation.
        """
        found = self._handoffs.pop((chat_id or "", thread_id, user), None)
        if found is None:
            return None
        seat, when = found
        if self._clock() - when > timedelta(seconds=HANDOFF_SECONDS):
            return None
        return seat

    async def _ask_for_text(
        self, label: str, chat_id: str, thread_id: int | None, user: str
    ) -> None:
        """Ask what to send, with the reply box already open.

        `force_reply` asks the client to aim the next message at this one, so
        the answer arrives with the question attached — and the question names
        the seat. That is the exact path, and it is preferred when it works.

        It is a request, though, not a guarantee, and a client that ignores it
        sends a sentence that looks like any other. So the seat is also held
        here for a few minutes. Belt and braces on purpose: the cost of the
        belt slipping was a message reaching an agent nobody chose, twice.
        """
        seat = find(self._seats, label)
        if seat is None:
            await self._say(
                f"No seat called <b>{html.escape(label)}</b>.\n\n" + self._seat_list(),
                chat_id,
                thread_id,
            )
            return
        # No `selective`. That flag limits a forced reply to people named in the
        # text or the author of the message being replied to — and this question
        # names nobody and replies to nothing, so it opened the reply box for no
        # one at all.
        self._remember_handoff(seat.label, chat_id, thread_id, user)
        await self._say(
            ASK_FOR_TEXT.format(seat=html.escape(seat.label)),
            chat_id,
            thread_id,
            reply_markup={"force_reply": True},
        )

    # --- opening what is not running ----------------------------------------

    async def _open_application(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/open claude` — start an agent that is not running.

        No approval card. Opening an application changes nothing that has to be
        undone, and putting a card in front of it would make the fast thing slow
        for no safety bought — the gate is for what an agent does once it is
        open, which is exactly where the cards already are.
        """
        if not desktop.available():
            await self._say(
                "This machine cannot open applications — that is macOS only.", chat_id, thread_id
            )
            return

        catalogued = catalogue.known()
        if not typed:
            await self._offer_applications(catalogued, chat_id, thread_id)
            return

        app = catalogue.resolve(typed)
        if app is None:
            # Said, then asked. A name nobody knows is usually a name typed from
            # memory, and the useful reply is the list of real ones as buttons.
            await self._say(
                f"I do not know an application called <b>{html.escape(typed)}</b>.",
                chat_id,
                thread_id,
            )
            await self._offer_applications(catalogued, chat_id, thread_id)
            return

        where = await asyncio.to_thread(desktop.status, app)
        if not where.installed:
            await self._say(
                f"<b>{html.escape(app.name)}</b> is not installed on this machine.",
                chat_id,
                thread_id,
            )
            return
        if where.on_screen:
            await self._say(
                f"\u2705 <b>{html.escape(app.name)}</b> is already open.", chat_id, thread_id
            )
            return

        # Running but not on screen is the interesting third state, and reading
        # it as "already open" is what once reported an application that was
        # nowhere to be seen. An editor whose last window is closed keeps its
        # process, its helpers and its language server alive, so `is running`
        # says true and there is still nothing to look at. Opening it is exactly
        # what somebody wants here.
        waking = " It was running with no window." if where.running else ""

        if await asyncio.to_thread(desktop.open_, app):
            # "Asked to open", not "open". `open` returns once macOS has taken
            # the request, and a cold application takes seconds more to appear —
            # claiming otherwise would be a promise this cannot keep.
            await self._say(
                f"\U0001f680 Asked macOS to open <b>{html.escape(app.name)}</b>.{waking}",
                chat_id,
                thread_id,
            )
        else:
            await self._say(
                f"\U0001f6ab Could not open <b>{html.escape(app.name)}</b>.", chat_id, thread_id
            )

    async def _offer_applications(
        self, catalogued: list, chat_id: str, thread_id: int | None
    ) -> None:
        """Ask which one, with a button each.

        Because `/open` arrives bare. Telegram's own command menu pastes the
        command and stops, so the useful next thing is the question rather than
        a list somebody then has to type from — the same reason `/to` offers
        seats instead of printing them.

        Only what can actually be opened is offered: installed, and not already
        on screen. A button that answers "it is already open" is a button that
        wasted a tap.
        """
        standing = [(app, await asyncio.to_thread(desktop.status, app)) for app in catalogued]
        worth = [app.name for app, where in standing if where.installed and not where.on_screen]
        keyboard = cards.open_choices(tuple(worth))

        if keyboard is None:
            installed = [app for app, where in standing if where.installed]
            await self._say(
                "\u2705 Everything is already open."
                if installed
                else "Nothing openable is installed on this machine.",
                chat_id,
                thread_id,
            )
            return

        await self._say("Open which one?", chat_id, thread_id, reply_markup=keyboard)

    # --- running a project's own commands -----------------------------------

    def _elapsed(self, seconds: float) -> str:
        """`3m 12s`. A bare count of seconds stops being readable at about 90."""
        if seconds < 90:
            return f"{seconds:.0f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"

    async def _run_command(self, typed: str, chat_id: str, thread_id: int | None) -> None:
        """`/command` — offer this project's commands, or start the named one."""
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(
                "I do not know which repository this chat is about. Give the "
                "project a <code>path:</code> in <code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return

        listed = commands_offered.offered(found.commands)
        if not listed:
            await self._say(
                f"<b>{html.escape(found.name)}</b> lists no commands. Add a "
                "<code>commands:</code> block to it in <code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return

        if not typed:
            await self._say(
                "Run which one?",
                chat_id,
                thread_id,
                reply_markup=cards.command_choices(tuple(c.name for c in listed)),
            )
            return

        command = commands_offered.resolve(found.commands, typed)
        if command is None:
            await self._say(
                f"<b>{html.escape(found.name)}</b> has no command called "
                f"<b>{html.escape(typed)}</b>.",
                chat_id,
                thread_id,
            )
            await self._run_command("", chat_id, thread_id)
            return

        # One at a time per project. Two `make` runs in one directory fight over
        # the same build outputs, and the second one's failure is a mystery.
        if busy := self._working.get(found.name):
            await self._say(
                f"\u23f3 <b>{html.escape(busy)}</b> is still running in "
                f"<b>{html.escape(found.name)}</b>. One at a time.",
                chat_id,
                thread_id,
            )
            return

        self._working[found.name] = command.name
        await self._say(
            f"\u25b6\ufe0f Running <code>{html.escape(command.line)}</code>\u2026",
            chat_id,
            thread_id,
        )
        # Detached, because this can run for the better part of an hour and the
        # poller has approval cards to keep delivering while it does.
        task = asyncio.create_task(
            self._carry_out(found.name, found.path, command, chat_id, thread_id)
        )
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _carry_out(
        self, project: str, path: Path, command, chat_id: str, thread_id: int | None
    ) -> None:
        """Run it to the end, then say what happened."""
        try:
            result = await asyncio.to_thread(commands_running.run, command.line, path)
        except Exception:
            logger.warning("A command from Telegram could not be run", exc_info=True)
            await self._say(
                f"\U0001f6ab <b>{html.escape(command.name)}</b> could not be started.",
                chat_id,
                thread_id,
            )
            return
        finally:
            # Released whatever happened. A project left marked busy by a crash
            # would refuse every command afterwards for no reason anybody could see.
            self._working.pop(project, None)

        if result.timed_out:
            head = f"\u23f1 <b>{html.escape(command.name)}</b> was stopped after "
        elif result.ok:
            head = f"\u2705 <b>{html.escape(command.name)}</b> finished in "
        else:
            head = f"\U0001f6ab <b>{html.escape(command.name)}</b> failed after "
        said = head + self._elapsed(result.seconds) + "."
        if result.output:
            said += f"\n\n<pre>{html.escape(result.output)}</pre>"
        await self._say(said, chat_id, thread_id)

    # --- committing what an agent wrote ------------------------------------
    #
    # Thin on purpose. Everything that decides anything — what git is asked,
    # what may be committed, whether a proposal is still answerable — lives in
    # `halyard.commits`. This resolves which repository a chat is about, moves
    # text between that package and Telegram, and nothing else.

    def _repository_for(self, chat_id: str) -> Project | None:
        """The project this chat is about, and where its code is.

        No configuration of its own: the chat already names a seat and a seat
        already names its project. A machine describing exactly one project
        answers for it from any chat, which is the single-seat setup that
        existed before seats were split across groups.
        """
        seat = for_chat(self._seats, chat_id) if chat_id else None
        name = seat.project if seat and seat.project else None
        if name is None and len(self._repositories) == 1:
            name = next(iter(self._repositories))
        found = self._repositories.get(name or "")
        return found if found and found.path else None

    def _message_runner(self, chat_id: str):
        """Whichever runtime this chat's seat uses, for the one-shot turn.

        The only place the commit flow touches a runtime at all, and it stays
        on this side of the boundary: `halyard.commits` is handed a finished
        sentence, never a way to ask for one.
        """
        seat = for_chat(self._seats, chat_id) if chat_id else None
        if seat and (found := self._runners.get(seat.runtime)):
            return found
        return self._runner

    async def _write_message(self, chat_id: str, work) -> tuple[str | None, tuple[str, ...]]:
        """Have the work described: a subject line, and what actually changed.

        Both in one turn. The second half is the point of asking at all — the
        person deciding is away from the desk and has not seen this code, and a
        list of filenames says where an agent has been rather than what it did.

        Fails soft, on purpose. A model that cannot be reached should not cost
        somebody the commit — the reference computed from the branch is a
        usable message on its own, and `Rewrite` is one tap away.
        """
        runner = self._message_runner(chat_id)
        said = None
        if runner is not None and hasattr(runner, "ask"):
            try:
                said = await asyncio.wait_for(
                    runner.ask(commits.prompt(work), model=MESSAGE_MODEL),
                    timeout=MESSAGE_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning("Could not have a commit message written", exc_info=True)
        said = said or ""
        return commits.assemble(work.reference, said) or None, commits.summary_of(said)

    async def _propose_commit(self, chat_id: str, thread_id: int | None) -> None:
        """`/commit` — read the branch's uncommitted work and ask about it."""
        found = self._repository_for(chat_id)
        if found is None:
            await self._say(
                "I do not know which repository this chat is about. Give the "
                "project a <code>path:</code> in <code>halyard.yaml</code>.",
                chat_id,
                thread_id,
            )
            return
        project, path = found.name, found.path

        # Off the event loop: git is a subprocess, and the poller has approval
        # cards to keep delivering while this reads a repository.
        work = await asyncio.to_thread(commits.read, path, project)
        if work.blocked:
            await self._say(f"\U0001f6ab {html.escape(work.blocked)}", chat_id, thread_id)
            return

        # Said before it starts, not after. A project's own check can run for
        # minutes, and silence for minutes reads as nothing having happened.
        if found.validate:
            await self._say(
                f"\u23f3 Running <code>{html.escape(found.validate)}</code>\u2026",
                chat_id,
                thread_id,
            )
        checked = await asyncio.to_thread(
            partial(commits.check, work, path, found.validate, warn_if=found.warn_if)
        )
        if checked.refused:
            # No card at all. A failing check is a fact rather than a judgement,
            # so there is nothing here for somebody to weigh.
            said = (
                f"\U0001f6ab <code>{html.escape(checked.refused)}</code> failed. "
                "Nothing was committed."
            )
            if checked.output:
                said += f"\n\n<pre>{html.escape(checked.output)}</pre>"
            await self._say(said, chat_id, thread_id)
            return

        message, summary = await self._write_message(chat_id, work)
        if not message:
            await self._say(
                "Could not write a commit message, and this branch is not named "
                "for an issue to fall back on. Commit this one at the desk.",
                chat_id,
                thread_id,
            )
            return

        handle = self._proposals.add(project, path, work, message, summary, checked.warnings)
        await self._say(
            commit_card.render(
                project=project,
                work=work,
                message=message,
                summary=summary,
                warnings=checked.warnings,
            ),
            chat_id,
            thread_id,
            reply_markup=commit_card.keyboard(handle),
        )

    async def _rewrite_commit(
        self, handle: str, text: str, chat_id: str, thread_id: int | None, user: str
    ) -> None:
        """Replace a proposed message with one somebody typed.

        Deliberately not a commit. The card comes back with the new wording and
        the same buttons, because the invariant worth keeping is that exactly
        one thing in this flow commits, and it is the Commit button — a typo
        typed on a phone should not be a commit nobody agreed to.
        """
        proposal = self._proposals.peek(handle)
        if proposal is None:
            await self._say("That commit is no longer open.", chat_id, thread_id)
            return
        written = commits.assemble(proposal.work.reference, text)
        if not written:
            await self._say("That message is empty.", chat_id, thread_id)
            return
        reworded = self._proposals.reword(handle, written)
        if reworded is None:
            await self._say("That commit is no longer open.", chat_id, thread_id)
            return
        await self._say(
            commit_card.render(
                project=reworded.project,
                work=reworded.work,
                message=reworded.message,
                summary=reworded.summary,
                warnings=reworded.warnings,
            ),
            chat_id,
            thread_id,
            reply_markup=commit_card.keyboard(handle),
        )

    async def _decide_commit(
        self, proposed: tuple[str, str], user_id: str, query_id: str, callback: dict
    ) -> None:
        """A button under a commit card."""
        handle, action = proposed
        # Checked exactly as an approval is. This one writes to a repository.
        if user_id not in self._authorized:
            await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
            logger.warning("Ignoring a commit button from unauthorized user %s", user_id)
            await self._dismiss(query_id)
            return

        message = callback.get("message") or {}
        here = str((message.get("chat") or {}).get("id") or "") or None
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")

        if action == commit_card.REWRITE:
            # Left in place: the sentence still has to find it.
            if handle not in self._proposals:
                await self._dismiss(query_id, "That commit is no longer open.")
                return
            await self._dismiss(query_id)
            await self._say(
                ASK_FOR_MESSAGE.format(handle=handle),
                here or "",
                thread_id,
                reply_markup={"force_reply": True},
            )
            return

        # Taken rather than read. That is what stops a second tap making a
        # second commit; there is no nonce to check.
        proposal = self._proposals.take(handle)
        if proposal is None:
            await self._dismiss(query_id, "That commit is no longer open.")
            return

        if action == commit_card.DROP:
            await self._dismiss(query_id, "Cancelled.")
            await self._settle_commit(proposal, "\u2716\ufe0f CANCELLED", user_id, here, message_id)
            return

        try:
            sha = await asyncio.to_thread(commits.commit, proposal.path, proposal.message)
        except Exception as refused:
            logger.warning("A commit from Telegram failed", exc_info=True)
            await self._dismiss(query_id, "git refused.")
            await self._say(
                f"\U0001f6ab git refused: {html.escape(str(refused))}", here or "", thread_id
            )
            return

        # Said out loud, not only by editing the card. A toast disappears and an
        # edit two screens up is easy to scroll past; the one thing somebody
        # needs to leave with is that it happened, and what it is called.
        branch = proposal.work.branch
        done = [
            f"\u2705 <b>Committed</b> <code>{html.escape(sha)}</code> "
            f"on <code>{html.escape(branch)}</code>",
            "",
            f"<pre>{html.escape(proposal.message)}</pre>",
        ]
        outcome = f"\u2705 COMMITTED {sha}"

        if action == commit_card.SEND:
            await self._dismiss(query_id, f"Committed {sha}, pushing\u2026")
            try:
                where = await asyncio.to_thread(commits.push, proposal.path, branch)
            except Exception as refused:
                logger.warning("A push from Telegram failed", exc_info=True)
                # The commit is made and safe; only the push failed. Saying
                # which is the difference between "tap it again" and "somebody
                # else moved the branch, go to a desk".
                done += ["", f"\U0001f6ab but the push failed: {html.escape(str(refused))}"]
                outcome = f"\u2705 COMMITTED {sha} \u2014 not pushed"
            else:
                done[0] += f"\n\U0001f680 <b>Pushed</b> to <code>{html.escape(where)}</code>"
                outcome = f"\U0001f680 PUSHED {sha}"
        else:
            await self._dismiss(query_id, f"Committed {sha}")

        await self._say("\n".join(done), here or "", thread_id)
        await self._settle_commit(proposal, outcome, user_id, here, message_id)

    async def _settle_commit(
        self,
        proposal,
        outcome: str,
        user_id: str,
        chat_id: str | None,
        message_id: int | None,
    ) -> None:
        """Edit the card to say what happened, so scrolling back is honest."""
        if message_id is None:
            return
        try:
            await self._api.edit_message_text(
                chat_id or self._chat_id,
                message_id,
                commit_card.render_resolved(
                    project=proposal.project,
                    message=proposal.message,
                    outcome=outcome,
                    by=f"tg:{user_id}",
                ),
                reply_markup=None,
            )
        except Exception:
            logger.warning("Could not update a commit card", exc_info=True)

    async def _offer_seats(
        self, text: str, chat_id: str, thread_id: int | None, anchor_id: int | None
    ) -> None:
        """Ask which seat, attached to the message that holds the text.

        Sent as a reply on purpose. Pressing a button then reads the exact
        message back out of the callback rather than out of the preview above
        it, which is shortened for reading — and a button that sends a
        *truncated* version of what it is shown next to would be worse than no
        button.
        """
        keyboard = cards.seat_choices(tuple(seat.label for seat in self._seats))
        if keyboard is None:
            await self._say(self._seat_list(), chat_id, thread_id)
            return
        preview = text if len(text) <= 200 else text[:200] + "…"
        await self._say(
            f"Send this to which seat?\n\n<blockquote>{html.escape(preview)}</blockquote>",
            chat_id,
            thread_id,
            reply_markup=keyboard,
            reply_to_message_id=anchor_id,
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
        if not (self._runner or self._runners) or self._registry is None:
            await self._say(
                "This control plane cannot send messages into a session. That needs "
                "the agent CLI, so it has to run on the host rather than in a container.",
                chat_id,
                thread_id,
            )
            return

        found = await self._session_for(chat_id)
        if found is not None and found.runner.busy(found.session_id):
            # The runner serialises per session, so this would sit in silence
            # until the turn before it finished. Silence is what makes people
            # think a message was lost.
            await self._say(
                "⏳ Still working on the last one — yours is queued behind it.",
                chat_id,
                thread_id,
            )
        if found is None:
            # Name the chat. Without it this says a seat is missing without
            # saying which one to add, and the id it wants is the one piece of
            # information nobody can look up from where they are standing — it
            # is not shown anywhere in Telegram's own interface.
            seat = self._seat_for_chat(chat_id)
            await self._say(
                (
                    f"No seat owns this chat (<code>{chat_id}</code>). Add it to a "
                    "seat's <code>chat:</code> in your seat configuration, then "
                    "restart — seats are read at startup."
                )
                if seat is None
                else (
                    f"The <b>{seat.label}</b> seat owns this chat, but "
                    f"{seat.runtime} has no session named "
                    f"<code>{seat.session}</code>. Check it with "
                    "<code>halyard doctor</code>."
                ),
                chat_id,
                thread_id,
            )
            return

        task = asyncio.create_task(self._deliver(found, text, actor, chat_id, thread_id))
        # Held so the loop does not drop the only reference and cancel it.
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _session_for(self, chat_id: str) -> _SessionTarget | None:
        """Which runtime-owned session a chat belongs to.

        The configured name is tried first. It is addressable from a standing
        start — a control plane that restarted a second ago can still find the
        session — whereas the registry only knows what has fired a hook since it
        came up. Telling somebody to go run a command somewhere before they can
        send a message is not an answer.
        """
        # The seat that owns this group, if one does. That is the whole
        # routing rule in this direction: a group is a seat, a seat knows its
        # runtime and its session, and nothing has to be worked out per message.
        seat = self._seat_for_chat(chat_id)
        role = seat.role if seat else self._role_for_chat(chat_id)

        name = seat.session if seat else (role and self._session_names.get(role))
        if name:
            found = await self._resolve(seat, name)
            if found:
                runner = self._runner_for(seat)
                if runner is not None:
                    return _SessionTarget(found.session_id, self._project, found.cwd, runner)
            logger.warning(
                "No session named %r for the %s seat", name, seat.label if seat else role
            )

        session = (
            await self._registry.latest_for_role(role)
            if role is not None
            else await self._registry.latest()
        )
        if session is None:
            return None
        runner = self._runners.get(session.agent_id) or self._runner_for(seat)
        if runner is None:
            return None
        return _SessionTarget(session.session_id, session.project, session.cwd, runner)

    def _seat_for_chat(self, chat_id: str) -> Seat | None:
        return for_chat(self._seats, chat_id) if self._seats else None

    def _runner_for(self, seat: Seat | None):
        """The runtime a seat is, falling back to the only one there is."""
        return (seat and self._runners.get(seat.runtime)) or self._runner

    async def _resolve(self, seat: Seat | None, name: str):
        """Ask that seat's runtime what the name means.

        The channel used to import Claude Code's lookup directly, which made a
        second runtime impossible to add without editing this file — and would
        have gone looking for a Codex thread in `~/.claude`.
        """
        runner = self._runner_for(seat)
        if runner is None:
            return None
        return await asyncio.to_thread(runner.resolve, name)

    def _role_for_chat(self, chat_id: str) -> Role | None:
        for role, destination in self._routes.items():
            if destination and destination[0] == chat_id:
                return role
        return None

    async def _deliver(
        self,
        session: _SessionTarget,
        text: str,
        actor: str,
        chat_id: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        session_id, project, cwd = session.session_id, session.project, session.cwd
        delivered = False
        try:
            delivered = await session.runner.send(session_id, text, cwd)
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
            # Name what was tried. "Check the log" is the message this project
            # keeps having to replace: the person reading it is on a phone,
            # away from the machine, and the one fact they cannot recover from
            # there is which session this went to and under which runtime.
            # Two runtimes can hold one name, so neither half is enough alone.
            runtime = getattr(session.runner, "id", "?")
            because = getattr(session.runner, "last_error", lambda _: None)(session_id)
            # The runtime usually said why, on a stream this used to discard.
            # "Not logged in · Please run /login" was printed by the CLI, thrown
            # away, and replaced with an instruction to read a log on a machine
            # the person had walked away from.
            detail = (
                f"\n\n<pre>{html.escape(because[:300])}</pre>"
                if because
                else "\n\nNothing was printed. <code>halyard doctor</code> checks the rest."
            )
            await self._say(
                f"⚠️ That did not reach <b>{html.escape(str(session_id))}</b> "
                f"({html.escape(str(runtime))}).{detail}",
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

        Only for turns started from here. Without an override, a resumed Claude
        session inherits its existing model. Other runtimes answer through the
        same preference interface with their own defaults.
        """
        seat = self._seat_for_chat(chat_id or "")
        runner = self._runner_for(seat)
        if runner is None:
            await self._say("No runner: this control plane cannot start turns.", chat_id, thread_id)
            return

        found = await self._session_for(chat_id or "")
        if found is None:
            await self._say("No session for this chat.", chat_id, thread_id)
            return
        session_id = found.session_id
        # Use the runner carried by the resolved target. The registry fallback
        # may know the runtime even when the chat has no configured seat.
        runner = found.runner

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
        allowed, enforced = runner.options(session_id).get(what, ((), False))
        if value and enforced and value.lower() not in allowed:
            await self._say(
                f"{what.capitalize()} is one of: <code>{' '.join(allowed)}</code>",
                chat_id,
                thread_id,
            )
            return

        if value:
            setter = runner.set_model if what == "model" else runner.set_effort
            cleared = value.lower() in ("default", "clear", "reset")
            setter(session_id, None if cleared else value)
            if cleared:
                model, effort = runner.preferences(session_id)
                back_to = model if what == "model" else effort
                answer = (
                    f"Cleared. Turns from here will use <b>{html.escape(back_to)}</b>."
                    if back_to
                    else (
                        f"Cleared. Turns from here will leave the {what} "
                        "to the resumed session/runtime."
                    )
                )
            else:
                answer = f"Turns started from here will use <b>{html.escape(value)}</b>."
            await self._say(answer, chat_id, thread_id)
            return

        model, effort = runner.preferences(session_id)
        chosen = model if what == "model" else effort
        role = seat.role if seat else self._role_for_chat(chat_id or "")
        wanted = seat.session if seat else self._session_names.get(role or Role.NAVIGATOR, "")
        ref = await self._resolve(seat, wanted or "")
        in_use = (ref.model if what == "model" else ref.effort) if ref else None
        lines = [f"<b>{what}</b>", f"  in the session: {html.escape(str(in_use or 'unknown'))}"]
        if chosen:
            lines.append(f"  from here: <b>{html.escape(chosen)}</b>")
        lines.append(f"\nSet with <code>/{what} &lt;value&gt;</code>, or <code>default</code>.")
        # Buttons when the runtime named a closed set, and the typed form
        # regardless: models are open-ended, so a name released this morning
        # has to work whether or not it is on a keyboard written months ago.
        await self._say(
            "\n".join(lines), chat_id, thread_id, reply_markup=cards.choices(what, allowed)
        )

    def _options(self, chat_id: str | None = None) -> str:
        """Everything that can be chosen, asked of the runtime rather than known.

        One message, because the question it answers — "what can I even say
        here?" — is asked from a phone, where reading a manual is not an option
        and a wrong guess costs a round trip.

        Nothing here is hardcoded in this module. A second runtime shows up in
        this output by existing, and a model list updated in the environment
        appears without a release.
        """
        runner = self._runner_for(self._seat_for_chat(chat_id or ""))
        if runner is None:
            return "No runner: this control plane cannot start turns."

        lines = [f"<b>{html.escape(runner.id)}</b>"]
        for name, (values, enforced) in runner.options().items():
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
        # Every seat, not every role: there are two drivers now, and telling
        # them apart is the point of listing them at all.
        configured = self._seats or [
            Seat(label=role.value, runtime=default_runtime(), session=name, role=role)
            for role, name in self._session_names.items()
        ]

        lines: list[str] = []
        for seat in configured:
            name = seat.session
            if not name:
                continue
            ref = await self._resolve(seat, name)
            label = f"{seat.label} ({seat.runtime}): <b>{html.escape(name)}</b>"
            if ref is None:
                lines.append(f"  {label} — not found")
                continue
            details = " · ".join(filter(None, [ref.model, ref.effort and f"effort {ref.effort}"]))
            seat_runner = self._runner_for(seat)
            busy = " · ⏳ working" if seat_runner and seat_runner.busy(ref.session_id) else ""
            lines.append(f"  {label}\n     at the desk: {html.escape(details) or 'unknown'}{busy}")
            if seat_runner is not None:
                model, effort = seat_runner.preferences(ref.session_id)
                mine = " · ".join(filter(None, [model, effort and f"effort {effort}"]))
                lines.append(
                    f"     from here: {html.escape(mine) or 'inherits the session/runtime'}"
                )
        return lines

    async def _say(
        self,
        text: str,
        chat_id: str | None = None,
        thread_id: int | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Answer in the conversation that asked.

        Not in the configured default chat, which is where this used to go: ask
        the navigator group something and the reply appeared in a private chat
        with the bot, which reads as the message having been lost.
        """
        try:
            await self._api.send_message(
                chat_id or self._chat_id,
                text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            logger.warning("Could not answer a command", exc_info=True)

    async def _handle_callback(self, callback: dict) -> None:
        query_id = str(callback.get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))

        chosen = cards.parse_choice_data(callback.get("data") or "")
        if chosen is not None:
            # Checked exactly as an approval is. Setting the model for a seat
            # is not an approval, but it is still an action taken on somebody
            # else's session, and the button is visible to a whole group.
            if user_id not in self._authorized:
                await self._record(unauthorized_callback(actor=f"tg:{user_id}", channel="telegram"))
                await self._dismiss(query_id)
                return
            message = callback.get("message") or {}
            here = str((message.get("chat") or {}).get("id") or "") or None
            what, value = chosen
            await self._dismiss(query_id)
            if what == "run":
                await self._run_command(value, here or "", message.get("message_thread_id"))
                return
            if what == "open":
                await self._open_application(value, here or "", message.get("message_thread_id"))
                return
            if what == "to":
                carried = ((message.get("reply_to_message") or {}).get("text") or "").strip()
                # The anchor may be the command itself, in which case the text
                # is everything after `/to`.
                if carried.startswith("/to"):
                    carried = carried.partition(" ")[2].strip()
                if not carried:
                    # No message to carry — the seat was picked from a bare
                    # `/to`. Ask for the text, naming the seat in the question
                    # so the answer arrives knowing where it goes.
                    await self._ask_for_text(
                        value, here or "", message.get("message_thread_id"), user_id
                    )
                    return
                await self._forward_to_seat(
                    f"{value} {carried}",
                    f"tg:{user_id}",
                    here or "",
                    message.get("message_thread_id"),
                )
                return
            await self._choose(what, value, here, message.get("message_thread_id"))
            return

        asked = cards.parse_question_data(callback.get("data") or "")
        if asked is not None:
            await self._answer_question(asked, user_id, query_id)
            return

        proposed = commit_card.parse_callback_data(callback.get("data") or "")
        if proposed is not None:
            await self._decide_commit(proposed, user_id, query_id, callback)
            return

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
            await self.send_long_content(
                request.session_id,
                request.command_full,
                "Full command",
                request.role,
                agent_id=request.agent_id,
                session_name=request.session_name,
            )
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

    async def _answer_question(
        self, asked: tuple[str, str, int], user_id: str, query_id: str
    ) -> None:
        """Resolve a question from a tapped option."""
        handle, nonce, index = asked
        entry = self._open_questions.get(handle)
        request_id = entry[0].request_id if entry else None

        if user_id not in self._authorized:
            await self._record(
                unauthorized_callback(
                    actor=f"tg:{user_id}", request_id=request_id, channel="telegram"
                )
            )
            await self._dismiss(query_id)
            return

        if entry is None:
            await self._dismiss(query_id, "That question is no longer open.")
            return

        request, message_id, chat_id = entry
        if not 0 <= index < len(request.options):
            # A button that names an option the request does not have. Nothing
            # to answer with, and guessing would be the wrong kind of helpful.
            await self._dismiss(query_id)
            return

        answer = request.options[index].label
        await self._resolve_question(
            request, message_id, chat_id, nonce, answer, f"tg:{user_id}", handle, query_id
        )

    async def _answer_open_question_with_text(self, here: str, text: str, user_id: str) -> bool:
        """Answer the question open in this chat with a typed reply.

        The "Other" path the tool always offers, and the reason a question card
        says *reply with your own words*. The session is blocked on the question,
        so a message typed into this chat is the answer to it rather than a new
        turn — which could not be delivered anyway while the turn is paused.

        Returns whether a question was answered, so the caller knows not to treat
        the message as ordinary chat.
        """
        entry = self._oldest_open_question_in(here)
        if entry is None:
            return False
        handle, (request, message_id, chat_id) = entry
        # The request's own nonce: authorisation here is "an allowed user, and a
        # question open in the chat it was asked in", which the caller already
        # checked. The nonce guards a public button against replay; a typed
        # answer is not that.
        await self._resolve_question(
            request, message_id, chat_id, request.nonce, text, f"tg:{user_id}", handle, None
        )
        return True

    async def _resolve_question(
        self,
        request: QuestionRequest,
        message_id: int,
        chat_id: str,
        nonce: str,
        answer: str,
        actor: str,
        handle: str,
        query_id: str | None,
    ) -> None:
        """Record an answer and settle the card, whichever way it arrived."""
        if self._question_store is None:
            return
        try:
            await self._question_store.answer(
                request.request_id, nonce=nonce, answer=answer, decided_by=actor
            )
        except AlreadyAnsweredError:
            await self._record(replayed_callback(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id, "Already answered.")
            return
        except QuestionInvalidNonceError:
            await self._record(invalid_nonce(actor=actor, request_id=request.request_id))
            await self._dismiss(query_id)
            return
        except QuestionExpiredError:
            await self._settle_question_card(request, message_id, chat_id, None, None)
            self._open_questions.pop(handle, None)
            await self._dismiss(query_id, "Too late — that went back to the terminal.")
            return
        except UnknownQuestionError:
            self._open_questions.pop(handle, None)
            await self._dismiss(query_id, "That question is no longer open.")
            return

        self._open_questions.pop(handle, None)
        await self._settle_question_card(request, message_id, chat_id, answer, actor)
        await self._dismiss(query_id, "Answered.")

    async def _settle_question_card(
        self,
        request: QuestionRequest,
        message_id: int,
        chat_id: str,
        answer: str | None,
        by: str | None,
    ) -> None:
        try:
            await self._api.edit_message_text(
                chat_id,
                message_id,
                cards.render_question_resolved(request, answer=answer, by=by),
                reply_markup=None,
            )
        except Exception:
            logger.warning(
                "Could not update the question card for %s", request.request_id, exc_info=True
            )

    def _oldest_open_question_in(
        self, chat_id: str
    ) -> tuple[str, tuple[QuestionRequest, int, str]] | None:
        """The longest-waiting question routed to this chat, if any."""
        self._forget_expired_questions()
        candidates = [
            (handle, entry) for handle, entry in self._open_questions.items() if entry[2] == chat_id
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1][0].created_at)

    def _forget_expired_questions(self) -> None:
        now = self._clock()
        for handle in [h for h, (r, _, _) in self._open_questions.items() if now >= r.expires_at]:
            del self._open_questions[handle]

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
