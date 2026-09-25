"""`halyard upkeep` — jobs Halyard does about itself. See `halyard.upkeep`.

The runtime is chosen here, not in the job: the job needs a turn that can only
read what it is given, and asks for one by the capability rather than by name —
`answers_without_tools` — so a runtime that cannot promise it is refused, never
quietly used instead.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from halyard import upkeep
from halyard.core import grants
from halyard.upkeep import runs_advice

USAGE = """usage: halyard upkeep runs-advice <project> [--days N] [--evidence]

  runs-advice <project>  what the project could trust to run without a card,
                         from the cards it still asks: Halyard gathers the
                         evidence from its log, a model proposes, and you add
                         what you agree with, with `halyard rules add`
    --days N             how far back to read (14, or `days:` below)
    --evidence           print what the model would be given, and ask no model

Configured under `upkeep: runs-advice:` in halyard.yaml — model, effort, days,
prompt, timeout. Works with Halyard stopped, and changes nothing: every count is
Halyard's, and what it proposes is added only by you.
"""


def main(args: Sequence[str]) -> int:
    """`halyard upkeep …` — see `USAGE`."""
    words: list[str] = []
    days: int | None = None
    evidence_only = False
    items = list(args)
    while items:
        item = items.pop(0)
        if item == "--evidence":
            evidence_only = True
        elif item == "--days" and items and items[0].isdigit() and int(items[0]) > 0:
            days = int(items.pop(0))
        elif item.startswith("-"):
            print(USAGE, file=sys.stderr)
            return 2
        else:
            words.append(item)
    if len(words) != 2 or words[0] != "runs-advice":
        print(USAGE, file=sys.stderr)
        return 2

    try:
        settings = _settings()
        project = _project(words[1])
        job = upkeep.load("runs-advice")
    except ValueError as error:
        print(f"halyard upkeep: {error}", file=sys.stderr)
        return 2

    found = runs_advice.gather(
        settings.db_path,
        project,
        _rules(settings),
        days=days or job.days,
        refuse_commits=bool(settings.refuse_agent_commits),
    )
    text = runs_advice.render(found)
    if evidence_only:
        print(text, end="")
        return 0
    if not any(card.today == "card" for card in found.cards):
        print(text, end="")
        print("\nNothing in the window would still be a card, so there is nothing to propose.")
        return 0

    runner = _runner(settings)
    if not getattr(runner, "answers_without_tools", False):
        print(
            "halyard upkeep: the runtime here cannot take a turn without tools, so the "
            "evidence is not given to it. `--evidence` shows it without a model.",
            file=sys.stderr,
        )
        return 1
    model = job.model or settings.inspection_model or "sonnet"
    effort = job.effort or settings.inspection_effort
    prompt = (job.prompt or runs_advice.PROMPT).read_text(encoding="utf-8")
    still = sum(card.today == "card" for card in found.cards)
    print(f"Asking {model} about {still} cards…", file=sys.stderr, flush=True)
    # An empty directory to run in: the turn has no tools, and nothing it
    # could pick up from where it stands belongs in it.
    with tempfile.TemporaryDirectory(prefix="halyard-upkeep-") as nowhere:
        answer = asyncio.run(
            runner.ask(
                text,
                system=prompt,
                model=model,
                effort=effort,
                timeout=job.timeout,
                cwd=Path(nowhere),
                purpose="upkeep runs-advice",
                project=project.name,
            )
        )
    if not answer:
        print(
            "halyard upkeep: no answer — the model failed, or took longer than "
            f"{job.timeout:.0f}s. Nothing to propose.",
            file=sys.stderr,
        )
        return 1
    advice = runs_advice.advise(answer, found)
    if advice is None:
        print(answer)
        print(
            "\nhalyard upkeep: that answer is not in the shape asked for, so nothing in it was "
            "checked. Nothing to add from it.",
            file=sys.stderr,
        )
        return 1
    print(runs_advice.render_advice(found, advice), end="")
    return 0


def _settings():
    try:
        from halyard.config import Settings

        return Settings()
    except Exception as error:
        raise ValueError(f"the settings cannot be read: {error}") from None


def _project(name: str):
    from halyard.core.config_file import projects

    described = {project.name: project for project in projects()}
    if name not in described:
        known = ", ".join(sorted(described)) or "none"
        raise ValueError(f"no project {name!r} in halyard.yaml (known: {known})")
    if described[name].path is None:
        raise ValueError(f"project {name!r} has no `path:`, so its commands cannot be judged")
    return described[name]


def _rules(settings) -> grants.Rules:
    """This machine's rules, read the way the service reads them — a block
    that will not parse grants nothing, and is said."""
    from halyard.core import tools, writes

    loaded: dict[str, tuple[str, ...]] = {}
    for name, block in (("tools", tools), ("writes", writes)):
        try:
            loaded[name] = block.load()
        except ValueError as error:
            print(f"halyard upkeep: ignoring `{name}:` — {error}", file=sys.stderr)
            loaded[name] = ()
    return grants.Rules(
        tools=loaded["tools"],
        writes=loaded["writes"],
        reads=bool(settings.allow_risk_at_or_below),
    )


def _runner(settings):
    """The default runtime's runner, built as the service builds it."""
    from halyard.agents import registry

    spec = registry.get(registry.DEFAULT)
    return spec.runner(settings) if spec is not None else None
