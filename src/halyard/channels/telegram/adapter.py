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
import logging
from collections.abc import Callable
from datetime import UTC, datetime

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
from halyard.core.audit import AuditLog, invalid_nonce, replayed_callback, unauthorized_callback

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: How long a poll waits for something to happen before coming back empty.
POLL_TIMEOUT_SECONDS = 30

#: Backoff after a failed poll, so a Telegram outage does not become a tight
#: loop against their API.
POLL_RETRY_SECONDS = 3.0


def _default_clock() -> datetime:
    return datetime.now(UTC)


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
    ) -> None:
        self._api = api
        self._store = store
        self._audit = audit
        self._chat_id = chat_id
        self._authorized = authorized_user_ids
        self._clock = clock
        self._open: dict[str, tuple[ApprovalRequest, int]] = {}
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
        message = await self._api.send_message(self._chat_id, text, reply_markup=markup)
        message_id = int(message["message_id"])
        self._open[cards.handle_of(request)] = (request, message_id)
        return str(message_id)

    async def send_message(self, session_id: str, text: str) -> str:
        message = await self._api.send_message(self._chat_id, text)
        return str(message["message_id"])

    async def send_long_content(self, session_id: str, content: str, title: str) -> str:
        """Send something that will not fit in a message.

        As a file rather than a wall of split messages: a diff or a full command
        is something you want to be able to scroll and search, not reassemble
        from six chat bubbles.
        """
        if len(content) <= cards.MESSAGE_LIMIT - 100:
            return await self.send_message(session_id, f"<b>{title}</b>\n<pre>{content}</pre>")
        filename = f"{title.lower().replace(' ', '-')}.txt"
        result = await self._api.send_document(
            self._chat_id, filename, content.encode("utf-8"), caption=title
        )
        return str(result["message_id"])

    # --- listening ----------------------------------------------------------

    async def _poll_forever(self) -> None:
        """Keep asking Telegram for updates until cancelled.

        Survives errors. If this loop stopped, no approval could ever be
        answered — they would all sit until their deadline and then be denied.
        That is the safe failure, but it is silent, so a poll that keeps failing
        says so at ERROR rather than letting the system look healthy.
        """
        while True:
            try:
                updates = await self._api.get_updates(
                    offset=self._offset, timeout=POLL_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Telegram poll failed; approvals cannot be answered until it recovers"
                )
                await asyncio.sleep(POLL_RETRY_SECONDS)
                continue

            for update in updates:
                self._offset = int(update["update_id"]) + 1
                callback = update.get("callback_query")
                if callback is None:
                    continue
                try:
                    await self._handle_callback(callback)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad callback must not take the loop down with it, or a
                    # single malformed update would silence every future answer.
                    logger.exception("Failed to handle a Telegram callback")

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

        request, message_id = entry

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
            await self._settle_card(request, message_id, "deny", None)
            await self._dismiss(query_id, "Too late — that expired and was denied.")
            return
        except UnknownApprovalError:
            # The store has evicted it, so nothing here can be resolved again.
            self._open.pop(handle, None)
            await self._dismiss(query_id, "That request is no longer open.")
            return

        await self._settle_card(request, message_id, decision.value, actor)
        await self._dismiss(query_id, "Allowed." if decision is Decision.ALLOW else "Denied.")

    # --- helpers ------------------------------------------------------------

    async def _settle_card(
        self, request: ApprovalRequest, message_id: int, decision: str, by: str | None
    ) -> None:
        """Rewrite the card to show the outcome and drop the buttons."""
        try:
            await self._api.edit_message_text(
                self._chat_id,
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
        for handle in [h for h, (r, _) in self._open.items() if now >= r.expires_at]:
            del self._open[handle]
