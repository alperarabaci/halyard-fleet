"""Connection retries must not duplicate a card Telegram may have accepted."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from halyard.channels.telegram.api import TelegramApi, TelegramError

TOKEN = "123456:SUPER-SECRET-BOT-TOKEN"


@pytest.fixture
def retry_delays(monkeypatch):
    delays: list[float] = []

    async def skip_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("halyard.channels.telegram.api.asyncio.sleep", skip_wait)
    return delays


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ConnectError])
async def test_connection_retries_deliver_the_same_card(error, retry_delays, caplog) -> None:
    sent: list[dict] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) < 3:
            raise error(f"Could not connect to {request.url}", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramApi(TOKEN, client=client)
        result = await api.send_message(
            "-100777", "Approve this command?", reply_markup={"inline_keyboard": []}
        )

    assert result == {"message_id": 42}
    assert len(sent) == 3
    assert sent[0] == sent[1] == sent[2]
    assert retry_delays == [1, 2]
    assert error.__name__ in caplog.text
    assert "attempt 3/3" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ConnectError])
async def test_connection_retries_stop_after_three_attempts(error, retry_delays) -> None:
    attempts = 0

    async def fail(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error("Telegram is unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        api = TelegramApi(TOKEN, client=client)
        with pytest.raises(error):
            await api.send_message("-100777", "Approve this command?")

    assert attempts == 3
    assert retry_delays == [1, 2]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    ],
)
async def test_other_transport_errors_do_not_resend_a_card(error, retry_delays) -> None:
    attempts = 0

    async def fail(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error("Delivery cannot be confirmed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        api = TelegramApi(TOKEN, client=client)
        with pytest.raises(error):
            await api.send_message("-100777", "Approve this command?")

    assert attempts == 1
    assert retry_delays == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}),
        httpx.Response(503, text="upstream error"),
    ],
)
async def test_api_errors_are_not_retried(response, retry_delays) -> None:
    attempts = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = TelegramApi(TOKEN, client=client)
        with pytest.raises(TelegramError):
            await api.send_message("-100777", "Approve this command?")

    assert attempts == 1
    assert retry_delays == []


async def test_cancelling_the_backoff_stops_before_another_send(monkeypatch) -> None:
    attempts = 0
    backoff_started = asyncio.Event()

    async def wait(delay: float) -> None:
        backoff_started.set()
        await asyncio.Event().wait()

    async def fail(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("Telegram is unreachable", request=request)

    monkeypatch.setattr("halyard.channels.telegram.api.asyncio.sleep", wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        api = TelegramApi(TOKEN, client=client)
        task = asyncio.create_task(api.send_message("-100777", "Approve this command?"))
        await asyncio.wait_for(backoff_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert attempts == 1
