"""Run the control plane: `halyard`, or `python -m halyard`."""

from __future__ import annotations

import logging

import uvicorn

from halyard.api.app import create_app
from halyard.config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
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
