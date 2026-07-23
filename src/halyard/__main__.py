"""Run the control plane: `halyard`, or `python -m halyard`."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from halyard.api.app import create_app
from halyard.config import Settings
from halyard.core.redaction import SecretRedactingFilter


def configure_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    max_bytes: int = 5_000_000,
    backups: int = 5,
) -> None:
    """Set up logging so a credential cannot get out through it.

    Two layers, because one is not enough. httpx logs every request line at
    INFO, and a Telegram bot token lives in the URL path — so the token was
    being written to the log once per poll, on a client whose `__repr__` had
    been overridden specifically to keep it out of tracebacks. Keeping a secret
    out of your own log lines is not the same as keeping it out of the log.

    Quieting httpx removes the known leak. The filter sits on the handler, so
    it covers every logger that reaches it, and is there for the next library
    that decides a URL is worth printing.

    **The file matters as much as the console.** A control plane is a service:
    it runs for days and nobody is watching the terminal at the moment
    something goes wrong. Without a file, working out what happened means
    reproducing it, and some of the things worth diagnosing are exactly the
    ones you cannot reproduce on demand. Rotation is there so an always-on
    service cannot fill a disk.

    A file that cannot be opened is a warning, not a failure. Losing the log is
    bad; refusing to run the gate because of it would be worse.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
                )
            )
        except OSError as error:
            print(f"halyard: cannot write to {log_file} ({error}); logging to console only")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # On each handler rather than the root logger: a filter on a logger does
    # not see records that propagate up to it from elsewhere. Every handler
    # needs its own, or the file gets the unredacted copy.
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactingFilter())


USAGE = """usage: halyard [command]

  (no command)  run the control plane
  serve         run the control plane
  doctor        check the configuration and say what is wrong with it
  sessions      list the session names this machine can see
  verify [rt]   prove the gate stops things, by running into it (costs turns)
  wire [dir]    put the gate on a project (merges; keeps a backup)
  unwire [dir]  take it off again, leaving everything else in place
"""


def main() -> None:
    """Dispatch a command, or refuse to guess.

    An unrecognised argument must not fall through to starting the server.
    A mistyped `halyard doctor.` once bound a port and connected to Telegram
    when it was asked to run a read-only check — a typo should produce a usage
    message, not a running service.
    """
    args = sys.argv[1:]
    command = args[0] if args else "serve"

    if command == "doctor":
        from halyard.doctor import run

        raise SystemExit(run())

    if command == "sessions":
        from halyard.doctor import sessions

        raise SystemExit(sessions())

    if command == "verify":
        from halyard.verify import RUNTIMES, verify

        wanted = args[1] if len(args) > 1 else None
        chosen = tuple(r for r in RUNTIMES if not wanted or r.name == wanted)
        if wanted and not chosen:
            names = ", ".join(r.name for r in RUNTIMES)
            print(f"halyard: unknown runtime {wanted!r}. Try one of: {names}", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(verify(runtimes=chosen or None))

    if command in ("wire", "unwire"):
        from halyard import wiring

        where = Path(args[1]).expanduser() if len(args) > 1 else Path.cwd()
        if not where.is_dir():
            print(f"halyard: {where} is not a directory", file=sys.stderr)
            raise SystemExit(2)
        action = wiring.wire if command == "wire" else wiring.unwire
        raise SystemExit(action(where.resolve()))

    if command not in ("serve",):
        print(f"halyard: unknown command {command!r}\n\n{USAGE}", file=sys.stderr)
        raise SystemExit(2)

    serve()


def _announce_the_rules(settings: Settings) -> None:
    """Say what running this commits you to, every time it starts.

    Not decoration. Each line below is something somebody has already been
    caught by, and none of them announce themselves at the moment they bite —
    a denied `ls` looks like a broken tool, a paused gate looks like a closed
    one, an expired approval looks like a command that hung.

    On startup rather than in the docs alone, because the person who wired this
    up and the person restarting it three weeks later are the same person with
    different amounts of memory.
    """
    print(
        f"""
────────────────────────────────────────────────────────────────────────
  Halyard is now the gate for every wired project. Three things to know:

  1. A wired project needs this process running.
     With Halyard down, a Bash command is DENIED — all of them, `ls`
     included. There is no terminal fallback to approve one with.
     To hand a project back:  halyard unwire <path>   (keeps a backup)

  2. It is live from the first command. Nothing to arm.
     Approvals go straight to Telegram. `/pause` stops that — and
     pausing needs this process running too.

  3. An approval expires after {settings.approval_timeout_seconds}s and is then DENIED.
     Not left waiting, not approved. A phone with notifications off is
     the same as saying no.

  `/pause` does not deny anything — it steps aside, and Claude Code's own
  permissions.allow list then runs matching commands without asking.
────────────────────────────────────────────────────────────────────────
"""
    )


def serve() -> None:
    # Settings first: the log file and level are read from them, and a
    # configuration error is readable on its own without a logger set up.
    settings = Settings()
    configure_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backups=settings.log_backups,
    )
    logger = logging.getLogger("halyard")
    logger.info(
        "Halyard Fleet starting on %s for project %r via channel %s",
        settings.bind,
        settings.project_name,
        settings.channel.value,
    )
    if settings.log_file is not None:
        logger.info("Logging to %s at %s", settings.log_file.resolve(), settings.log_level)
    _announce_the_rules(settings)
    if settings.channel.decides_without_a_human:
        logger.warning(
            "Channel %s answers every approval by itself. Nobody is being asked.",
            settings.channel.value,
        )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
