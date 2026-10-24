"""Every card with buttons, opened and closed in one place.

A card is a message with buttons under it. Each one used to be sent where it
was needed and closed — or not — by whatever handled its press, and every new
card was a new chance to forget: an approval that timed out kept its buttons, a
picker pressed once could be pressed again, and a handler that was given only
the button's value had no card to close at all.

The desk is where they all go through. It keeps what each card is and what it
said, and closes it by one of three rules:

- **Spent on press.** A picker closes the moment one of its buttons is pressed,
  saying which, before what the button does is done. See `STANDING` for the
  buttons that do not spend their card.
- **Closed with what it belongs to.** An approval, a question, a commit
  proposal: when that ends, its card says how and loses its buttons, whoever
  ended it and however.
- **Expired.** A card with a deadline is closed once the deadline passes, by the
  poll loop, if nothing closed it first.

Closing is cosmetic and idempotent. Whatever a card asked has been decided and
recorded elsewhere before the card is touched, so a card that cannot be edited
is untidy, never wrong — and closing one twice edits it once.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from halyard.channels.telegram.shelf import Kept, Shelf

logger = logging.getLogger(__name__)

#: The buttons that do not spend the card they are on: they act on a workflow
#: run, which goes on after the press, and the card is the run's way to be
#: steered again. Every other button is a choice made once.
STANDING = frozenset({"flowgo", "flowon", "flowpick", "flowstop", "flowto"})

#: How many closed cards are remembered, so a second close of the same message
#: — two paths ending one card, or a card from before a restart pressed twice —
#: edits it once.
REMEMBERED = 2000

#: What a card says when its deadline passed before anything closed it.
EXPIRED = "⌛ Expired"

#: What a card the last process left open says, closed as this one starts.
RESTARTED = "⏹ <i>Closed — Halyard restarted.</i>"

#: How long a card that could not be closed after a restart is tried again at
#: the next one. Past this it is let go: a message deleted, or one Telegram will
#: no longer let the bot edit, would otherwise be tried on every start.
KEPT_FOR = timedelta(days=2)

#: The pause between closing one left-over card and the next, so a restart with
#: many open runs into no rate limit of Telegram's.
PACE_SECONDS = 0.3


@dataclass
class Card:
    """One message with buttons, as the desk sent it."""

    chat: str
    message_id: int
    thread: int | None
    #: What kind of card: `approval`, `question`, `commit`, or the kind of its
    #: first button — `label`, `inspect`, `flow`…
    kind: str
    #: What it belongs to — an approval's request id, a commit's handle — so
    #: everything about one subject can be closed together. Empty for a card
    #: that belongs to nothing but itself.
    key: str
    #: What it said, as HTML, so closing keeps its formatting.
    html: str
    keyboard: dict
    expires_at: datetime | None = None
    #: The card's whole text once it has ended, given how — `expired`,
    #: `shutdown`, `allow` — and by whom, when that is more than a line under
    #: what it said: an approval says how it was decided, in place of the
    #: question it asked.
    ending: Callable[[str, str | None], str] | None = None


def kind_of(keyboard: dict | None) -> str:
    """The kind a keyboard's first button says: `hc:label:andon` is `label`,
    `hg:…` is `commit`. Empty when it has none."""
    for row in (keyboard or {}).get("inline_keyboard") or ():
        for button in row:
            data = str(button.get("callback_data") or "")
            prefix, _, rest = data.partition(":")
            if prefix == "hc":
                return rest.partition(":")[0]
            if prefix == "hg":
                return "commit"
            if prefix == "hf":
                return "approval"
            if prefix == "hq":
                return "question"
    return ""


def has_buttons(keyboard: dict | None) -> bool:
    """Whether a reply markup puts buttons under the message — an inline
    keyboard with at least one button. A forced reply is not a card."""
    return any((keyboard or {}).get("inline_keyboard") or ())


class CardDesk:
    """Opens cards, remembers them, and closes each exactly once."""

    def __init__(
        self,
        api,
        clock: Callable[[], datetime],
        *,
        shelf: Shelf | None = None,
        pace: float = PACE_SECONDS,
    ) -> None:
        self._api = api
        self._clock = clock
        self._cards: dict[tuple[str, int], Card] = {}
        self._closed: OrderedDict[tuple[str, int], None] = OrderedDict()
        #: Where open cards are written down, so the next start can close what
        #: this one leaves open. None keeps them in memory only.
        self._shelf = shelf
        self._pace = pace

    # --- opening -------------------------------------------------------------

    async def open(
        self,
        chat: str,
        text: str,
        keyboard: dict | None,
        *,
        thread: int | None = None,
        reply_to: int | None = None,
        kind: str = "",
        key: str = "",
        expires_at: datetime | None = None,
        ending: Callable[[str, str | None], str] | None = None,
    ) -> Card | None:
        """Send a message and, when it has buttons, keep it as a card.

        Raises whatever sending raised: whether a card that never arrived is a
        denial or a shrug is the caller's to decide. A message with no buttons
        is kept only when it is about something — `key` — as a question with
        nothing to tap still asks until it is answered or its deadline passes.
        None for any other message, which is not a card and is not kept.
        """
        message = await self._api.send_message(
            chat,
            text,
            reply_markup=keyboard,
            message_thread_id=thread,
            reply_to_message_id=reply_to,
        )
        if not has_buttons(keyboard) and not key:
            return None
        card = Card(
            chat=str(chat),
            message_id=int(message["message_id"]),
            thread=thread,
            kind=kind or kind_of(keyboard),
            key=key,
            html=text,
            keyboard=dict(keyboard or {}),
            expires_at=expires_at,
            ending=ending,
        )
        self._cards[(card.chat, card.message_id)] = card
        if self._shelf is not None:
            kept = Kept(card.chat, card.message_id, card.kind, key, text, self._clock())
            await asyncio.to_thread(self._shelf.put, kept)
            if (card.chat, card.message_id) in self._closed:
                # Closed while it was being written down: struck off again, or
                # the next start would close it a second time.
                await asyncio.to_thread(self._shelf.take, card.chat, card.message_id)
        return card

    # --- finding -------------------------------------------------------------

    def find(self, chat: str | None, message_id: int | None) -> Card | None:
        """The open card in that message, if the desk sent it and it is open."""
        if not chat or message_id is None:
            return None
        return self._cards.get((str(chat), int(message_id)))

    def of(self, kind: str, key: str) -> list[Card]:
        """Every open card about one subject — a request, a proposal."""
        return [card for card in self._cards.values() if card.kind == kind and card.key == key]

    def live(self) -> list[Card]:
        """Every card still open, oldest first."""
        return list(self._cards.values())

    @staticmethod
    def button(card: Card | None, data: str) -> str:
        """The label of the button carrying `data` on a card, or empty."""
        for row in (card.keyboard if card else {}).get("inline_keyboard") or ():
            for button in row:
                if button.get("callback_data") == data:
                    return str(button.get("text") or "")
        return ""

    # --- closing -------------------------------------------------------------

    async def close(
        self,
        chat: str | None,
        message_id: int | None,
        *,
        outcome: str | None = None,
        text: str | None = None,
        shown: str = "",
    ) -> bool:
        """Take the buttons off a card and say what became of it.

        `text` is the card's whole new text, for a card that is rewritten as
        what it ended as — an approval, a commit. `outcome` is one line of HTML
        under what the card said, kept as it was sent. `shown` is the message's
        own text as a button press carries it, for a card the desk did not send:
        one from before a restart, which is all there is to keep.

        Whether this call closed it: False when it was closed already.
        """
        if not chat or message_id is None:
            return False
        where = (str(chat), int(message_id))
        if where in self._closed:
            return False
        card = self._cards.pop(where, None)
        self._remember(where)
        if self._shelf is not None:
            await asyncio.to_thread(self._shelf.take, *where)
        if text is None:
            said = card.html if card is not None else html.escape(shown.strip())
            line = outcome or "✖️ Closed"
            text = f"{said}\n\n{line}" if said else line
        await self._edit(where, text)
        return True

    async def close_card(self, card: Card, **closing) -> bool:
        return await self.close(card.chat, card.message_id, **closing)

    async def close_about(self, kind: str, key: str, **closing) -> int:
        """Close every open card about one subject. How many."""
        closed = 0
        for card in self.of(kind, key):
            closed += await self.close_card(card, **closing)
        return closed

    async def end_about(self, kind: str, key: str, how: str, by: str | None = None) -> int:
        """Close every open card about one subject, each saying how it ended.
        How many — none when nothing about it was open."""
        closed = 0
        for card in self.of(kind, key):
            closed += await self._end(card, how, by)
        return closed

    async def sweep(self) -> int:
        """Close every card whose deadline has passed. How many.

        The backstop, not the usual way: whatever a card belongs to normally
        closes it as it ends. This is for what nothing closed — and it is why
        a card with a deadline cannot outlive it.
        """
        now = self._clock()
        expired = [
            card
            for card in self._cards.values()
            if card.expires_at is not None and now >= card.expires_at
        ]
        closed = 0
        for card in expired:
            closed += await self._end(card, "expired", None)
        return closed

    async def _end(self, card: Card, how: str, by: str | None) -> bool:
        if card.ending is not None:
            return await self.close_card(card, text=card.ending(how, by))
        return await self.close_card(card, outcome=EXPIRED if how == "expired" else None)

    async def left_over(self) -> list[Kept]:
        """The cards the last process left open — read before this one opens
        any, so none of its own is taken for one of them."""
        if self._shelf is None:
            return []
        return await asyncio.to_thread(self._shelf.everything)

    async def close_left_over(self, kept: list[Kept]) -> int:
        """Close the cards the last process left open. How many.

        An approval was denied as that process stopped, and anything else is
        better asked again than answered from a card left over from before. Each
        says so and loses its buttons. One Telegram would not let go of is tried
        again at the next start, for a while — see `KEPT_FOR`.
        """
        closed = 0
        for card in kept:
            where = (card.chat, card.message_id)
            if where in self._cards or where in self._closed:
                continue
            self._remember(where)
            said = f"{card.html}\n\n{RESTARTED}" if card.html else RESTARTED
            done = await self._edit(where, said)
            if done or self._clock() - card.opened_at > KEPT_FOR:
                await asyncio.to_thread(self._shelf.take, *where)
            closed += done
            await asyncio.sleep(self._pace)
        return closed

    def _remember(self, where: tuple[str, int]) -> None:
        self._closed[where] = None
        while len(self._closed) > REMEMBERED:
            self._closed.popitem(last=False)

    async def _edit(self, where: tuple[str, int], text: str) -> bool:
        """Rewrite the message without its buttons — or, when Telegram will not
        take the new text, at least take the buttons away. Whether either did.

        The text can be refused where the buttons cannot: a message grown past
        Telegram's limit by the line under it, or one somebody's client says is
        unchanged. A card whose text stays but whose buttons go is closed; one
        whose buttons stay is not.
        """
        chat, message_id = where
        try:
            await self._api.edit_message_text(chat, message_id, text, reply_markup=None)
            return True
        except Exception:
            logger.debug("Could not rewrite card %s in %s", message_id, chat, exc_info=True)
        try:
            await self._api.edit_message_reply_markup(
                chat, message_id, reply_markup={"inline_keyboard": []}
            )
            return True
        except Exception:
            # Cosmetic. What the card asked is decided and recorded already.
            logger.warning("Could not close card %s in %s", message_id, chat, exc_info=True)
            return False
