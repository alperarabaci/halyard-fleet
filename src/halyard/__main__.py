"""Run the control plane: `halyard`, or `python -m halyard`."""

from __future__ import annotations

import logging

import uvicorn

from halyard.api.app import create_app
from halyard.config import Settings
from halyard.core.redaction import SecretRedactingFilter


def configure_logging() -> None:
    """Set up logging so a credential cannot get out through it.

    Two layers, because one is not enough. httpx logs every request line at
    INFO, and a Telegram bot token lives in the URL path — so the token was
    being written to the log once per poll, on a client whose `__repr__` had
    been overridden specifically to keep it out of tracebacks. Keeping a secret
    out of your own log lines is not the same as keeping it out of the log.

    Quieting httpx removes the known leak. The filter sits on the handler, so
    it covers every logger that reaches it, and is there for the next library
    that decides a URL is worth printing.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # On the handler rather than the root logger: a filter on a logger does not
    # see records that propagate up to it from elsewhere.
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactingFilter())


def main() -> None:
    configure_logging()
    settings = Settings()
    logger = logging.getLogger("halyard")
    logger.info(
        "Halyard Fleet starting on %s for project %r via channel %s",
        settings.bind,
        settings.project_name,
        settings.channel.value,
    )
    if settings.channel.decides_without_a_human:
        logger.warning(
            "Channel %s answers every approval by itself. Nobody is being asked.",
            settings.channel.value,
        )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
