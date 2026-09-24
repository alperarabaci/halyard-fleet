"""`halyard inspect` — a kept inspection run given again to another model.

An experiment, and nothing in the work depends on it: no chat, no label, no
workflow. It reads the runs kept while `HALYARD_KEEP_INSPECTIONS` is on, gives
one of them to another model through `inspections.repeat`, and puts the runs
side by side. The runtime is chosen here, not in `inspections`, which reaches
a model only through `Asker`.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path

from halyard import frame
from halyard.core.config_file import Project, projects
from halyard.inspections import repeat as repeating

USAGE = """usage: halyard inspect <recent [n] | repeat <id> [options] | compare <id>>

  recent [n]         the last n inspections the work made (10), with their ids
  repeat <id>        give one the same input again, and keep the answer beside
    --model <m>        the original as an experiment: on this model,
    --effort <e>       thinking this hard (`default`: the runtime's own),
    --runtime <r>      on this runtime — `opencode`, say — with --model,
    --times <n>        n runs, one after another
  compare <id>       a run and its repeats side by side; the input and every
                     answer are written out as files, for whoever judges them

A repeat changes only what it is told to: a model, an effort or a runtime left
out is the original's — except that on another runtime the model has to be
named, and the effort is that runtime's own unless named too. An id can be cut
to any start of it no other id has. Runs are kept only while
HALYARD_KEEP_INSPECTIONS is on.

A repeat takes a turn of its own in the project's directory, as an inspection
does: a command its model asks to run comes to Telegram as the repeat's card,
and with Halyard stopped it is refused. On opencode it runs in the opencode
already open at the desk, in a session of its own that is deleted afterwards.
"""


def main(args: Sequence[str]) -> int:
    """`halyard inspect …` — see `USAGE`."""
    try:
        words, named = _options(args, known=("model", "effort", "times", "runtime"))
    except ValueError as wrong:
        print(f"halyard inspect: {wrong}\n\n{USAGE}", file=sys.stderr)
        return 2
    what, rest = (words[0], words[1:]) if words else ("", [])
    # How many words each takes after its name: `recent` a number or none, the
    # others one id.
    if len(rest) not in {"recent": (0, 1), "repeat": (1,), "compare": (1,)}.get(what, ()):
        print(USAGE, file=sys.stderr)
        return 2
    settings = _settings()
    database = settings.db_path if settings is not None else Path("./halyard.db")

    if what == "recent":
        if rest and not (rest[0].isdigit() and int(rest[0]) > 0):
            print(f"halyard inspect recent: {rest[0]!r} is not a number of runs", file=sys.stderr)
            return 2
        return _recent(database, int(rest[0]) if rest else 10)

    try:
        chosen = repeating.find(database, rest[0])
    except ValueError as several:
        print(f"halyard inspect: {several}", file=sys.stderr)
        return 2
    if chosen is None:
        print(
            f"halyard inspect: no run kept in {database} has an id starting {rest[0]!r}",
            file=sys.stderr,
        )
        return 2
    # A repeat's id stands for its original: that is what is given again, and
    # what everything else is compared with.
    original = (repeating.find(database, chosen.repeat_of) if chosen.repeat_of else None) or chosen
    if what == "compare":
        return _compare(database, original)
    return _repeat_command(original, named, settings, database)


def _options(args: Sequence[str], *, known: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Words in order, and `--name value` (or `--name=value`) pairs.

    An option this does not take is refused rather than passed over: a
    misspelt `--time 3` would otherwise run once and say nothing.
    """
    words: list[str] = []
    named: dict[str, str] = {}
    items = list(args)
    while items:
        item = items.pop(0)
        if not item.startswith("--"):
            words.append(item)
            continue
        name, equals, value = item[2:].partition("=")
        if name not in known:
            raise ValueError(f"there is no --{name}")
        if not equals:
            if not items:
                raise ValueError(f"--{name} needs a value")
            value = items.pop(0)
        named[name] = value
    return words, named


def _settings():
    """Halyard's own settings, read the way the service reads them — or None
    when they cannot be, as `halyard usage` does."""
    try:
        from halyard.config import Settings

        return Settings()
    except Exception:
        return None


def _line(run: repeating.Row, repeated: int = 0) -> str:
    """One run on one line: enough to pick it out, and what came of it."""
    ran_for = f" · {run.step}" if run.step else f" · {run.transition}" if run.transition else ""
    parts = [
        run.id[:8],
        f"{run.at.astimezone():%m-%d %H:%M}",
        run.work or run.project,
        f"{run.inspection}{ran_for}",
        f"{run.model}@{run.effort}" if run.effort else run.model,
        f"{run.took:.0f}s",
        run.outcome if run.answer is not None else f"unmeasured: {run.why}",
    ]
    if run.finding:
        parts.append(run.finding)
    if repeated:
        parts.append(f"repeated {repeated}x")
    return "  ".join(parts)


def _recent(database: Path, limit: int) -> int:
    runs = repeating.recent(database, limit)
    if not runs:
        print(
            f"No inspection runs are kept in {database}. "
            "They are kept while HALYARD_KEEP_INSPECTIONS is on."
        )
        return 0
    counts = repeating.repeats(database, [run.id for run in runs])
    for run in runs:
        print(_line(run, counts.get(run.id, 0)))
    return 0


def _folder(database: Path, original: repeating.Row) -> Path:
    """Where a comparison is written: beside the database, under the run's
    project, where the service keeps a project's agent prose — never inside
    the project's repository."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", original.project).strip(".") or "_"
    return database.parent / "projects" / safe / "inspections" / original.id


def _compare(database: Path, original: repeating.Row) -> int:
    runs = repeating.family(database, original.id)
    table = repeating.compared(database, runs)
    into = _folder(database, original)
    repeating.write(into, runs, table)
    print("\n".join(table))
    print(f"\nThe input, this table and every answer: {into}")
    return 0


class _Asking:
    """A runtime's turn of its own, as an inspection's `Asker` — its tokens
    recorded as a repeat's, apart from the inspections the work runs.

    Each turn's session is marked as Halyard's own with the running service
    before the turn begins, so its reply stays out of the chat, it labels no
    task, and a command it asks to run comes as the repeat's card. With no
    service running there is nobody to tell — and nothing to relay it either.
    """

    def __init__(self, runner, project: str, *, service: str | None = None) -> None:
        self._runner = runner
        self._project = project
        self._service = service

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 180.0,
        model: str | None = None,
        cwd: Path | None = None,
        name: str | None = None,
        edits: bool = True,
        session_id: str | None = None,
        effort: str | None = None,
    ) -> str | None:
        label = f"{name} · repeat" if name else "repeat"
        return await self._runner.ask(
            text,
            timeout=timeout,
            model=model,
            cwd=cwd,
            edits=edits,
            session_id=session_id,
            purpose=f"inspect {label}",
            project=self._project,
            effort=effort,
            started=lambda ident: self._started(ident, label),
        )

    async def _started(self, ident: str, label: str) -> None:
        # Shortened as its cards show it, so a card can be told for this run's.
        shown = ident if len(ident) <= 13 else f"{ident[:4]}…{ident[-4:]}"
        print(f"  session {shown}", flush=True)
        if self._service:
            await asyncio.to_thread(_mark_own, self._service, ident, label)

    @property
    def runtime(self) -> str:
        """Which runtime the turns run on, by its id."""
        return str(getattr(self._runner, "id", "") or "")


def _mark_own(service: str, ident: str, label: str) -> None:
    """Tell the running service a session is Halyard's own. Quiet when it is
    not running: then nothing it would have done with that session happens."""
    request = urllib.request.Request(
        f"{service}/v1/own",
        data=json.dumps({"session_id": ident, "label": label}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def _service(settings) -> str:
    """Where the running service answers, as `HALYARD_BIND` says."""
    host, _, port = str(getattr(settings, "bind", "127.0.0.1:8787")).rpartition(":")
    return f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{port}"


def _one_shot(settings, runtime: str):
    """That runtime's runner, built as the service builds it — its binary, its
    token, and what it used recorded in the same database — or None when it
    cannot take a turn of its own."""
    from halyard.agents import registry

    spec = registry.get(runtime)
    runner = spec.runner(settings) if spec is not None else None
    return runner if hasattr(runner, "ask") else None


def _repeat_command(
    original: repeating.Row, named: dict[str, str], settings, database: Path
) -> int:
    from halyard.agents import registry

    # What it is not told is the original's, so that one thing changes at a
    # time: another model at the same effort, or the same model thinking harder.
    # Another runtime is the exception: a model's name, or an effort, means
    # nothing to a runtime it was not written for.
    runtime = named.get("runtime", "").strip() or original.runtime or registry.DEFAULT
    moved = runtime != (original.runtime or registry.DEFAULT)
    model = named.get("model", "").strip() or ("" if moved else original.model)
    if not model:
        print(
            f"halyard inspect repeat: on {runtime}, with which model? "
            "--model zai-coding-plan/glm-5.3, for one",
            file=sys.stderr,
        )
        return 2
    effort = named.get("effort", "").strip().lower() or (None if moved else original.effort)
    if effort == "default":
        effort = None
    times = named.get("times", "1")
    if not (times.isdigit() and int(times) > 0):
        print(
            f"halyard inspect repeat: --times takes a number of runs, not {times!r}",
            file=sys.stderr,
        )
        return 2
    if settings is None:
        # The runner would be built without the database, and a repeat whose
        # tokens go unrecorded answers half of what it is run for.
        print(
            "halyard inspect repeat: Halyard's settings could not be read; "
            "`halyard doctor` says why",
            file=sys.stderr,
        )
        return 2
    try:
        found = next((p for p in projects() if p.name == original.project), None)
    except ValueError as unreadable:
        print(f"halyard inspect repeat: {unreadable}", file=sys.stderr)
        return 2
    if found is None or found.path is None:
        print(
            f"halyard inspect repeat: {original.project} has no `path:` in halyard.yaml to run in",
            file=sys.stderr,
        )
        return 2
    if runtime not in registry.names():
        print(
            f"halyard inspect repeat: there is no runtime {runtime!r}; "
            f"there is {', '.join(registry.names())}",
            file=sys.stderr,
        )
        return 2
    runner = _one_shot(settings, runtime)
    if runner is None:
        print(
            f"halyard inspect repeat: {runtime} cannot take a turn of its own here",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(
        _repeat(
            original,
            asker=_Asking(runner, found.name, service=_service(settings)),
            model=model,
            effort=effort,
            times=int(times),
            found=found,
            database=database,
        )
    )


async def _repeat(
    original: repeating.Row,
    *,
    asker: _Asking,
    model: str,
    effort: str | None,
    times: int,
    found: Project,
    database: Path,
) -> int:
    """One run after another, never side by side: every command one asks to
    run is a card somebody answers."""
    assert found.path is not None
    for turn in range(1, times + 1):
        context = await asyncio.to_thread(frame.context, found.path, found.name)
        session = str(uuid.uuid4())
        print(
            f"{original.inspection} · repeat {turn} of {times} with "
            f"{model}@{effort or 'default'} on {asker.runtime} in {found.path}",
            flush=True,
        )
        kept = await repeating.repeat(
            original,
            asker=asker,
            model=model,
            effort=effort,
            runtime=asker.runtime,
            project=found.path,
            context=context,
            findings=found.label_findings,
            database=database,
            session=session,
        )
        if kept is None:
            print(
                f"halyard inspect repeat: it ran, and could not be kept in {database}",
                file=sys.stderr,
            )
            return 1
        print(f"  {_line(kept)}", flush=True)
    print(f"\nSide by side: halyard inspect compare {original.id[:8]}")
    return 0
