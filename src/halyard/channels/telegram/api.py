"""A small async client for the parts of the Telegram Bot API this project uses.

Deliberately not a general-purpose wrapper. Five calls, no retry policy, no
model layer — just enough to put a card in front of somebody and hear back.

The token never appears in a log message. It lives in the base URL and nowhere
else, and `__repr__` is overridden so an exception traceback that happens to
include this object does not paste a bot token into the audit trail.
"""

from __future__ import annotations

from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """The Bot API refused a call."""


class TelegramApi:
    """Talks to the Bot API over HTTPS."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_root: str = API_ROOT,
    ) -> None:
        self._base = f"{api_root}/bot{token}"
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:
        # Never let the token reach a traceback or a log line.
        return "<TelegramApi>"

    async def open(self) -> None:
        if self._client is None:
            # Long polling holds a request open, so the read timeout has to
            # outlast the poll itself or every poll would look like a failure.
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=90.0))

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, **payload: Any) -> Any:
        if self._client is None:
            raise TelegramError("TelegramApi.open() must be awaited before use")
        body = {key: value for key, value in payload.items() if value is not None}
        response = await self._client.post(f"{self._base}/{method}", json=body)
        try:
            answer = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned a non-JSON body") from exc
        if not answer.get("ok"):
            raise TelegramError(f"{method} failed: {answer.get('description', 'unknown error')}")
        return answer.get("result")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
        message_thread_id: int | None = None,
    ) -> dict:
        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
    ) -> Any:
        return await self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> Any:
        """Dismiss the spinner on the pressed button.

        Called for every callback, including ones that are refused. Leaving it
        unanswered makes the button look broken rather than refused.
        """
        return await self._call(
            "answerCallbackQuery", callback_query_id=callback_query_id, text=text
        )

    async def send_document(
        self, chat_id: str, filename: str, content: bytes, *, caption: str | None = None
    ) -> dict:
        if self._client is None:
            raise TelegramError("TelegramApi.open() must be awaited before use")
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        response = await self._client.post(
            f"{self._base}/sendDocument",
            data=data,
            files={"document": (filename, content, "text/plain")},
        )
        answer = response.json()
        if not answer.get("ok"):
            raise TelegramError(f"sendDocument failed: {answer.get('description')}")
        return answer["result"]

    async def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict]:
        return await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["callback_query", "message"],
        )
