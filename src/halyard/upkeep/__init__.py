"""What Halyard does about itself, with its own tools.

Everything else here is about somebody's project. This is about Halyard: jobs
that read what it has kept — the audit log, the cards it asked — and put that
in front of a model with a question, so that looking after the gate is not a
program somebody writes each time a new question about it comes up.

**Not a project, on purpose.** Halyard under `projects:` would need exceptions
— nothing to wire, no agents, no chat — and each would be a place where a real
project's rule could go missing. So it has one block of its own, and a job is
a name under it:

    upkeep:
      runs-advice:
        model: opus
        effort: high
        days: 14

**A job answers in the terminal and changes nothing.** It reads the database
over a read-only connection, closed before any model is asked, and never talks
to the running service — so it works with the service and Telegram both off.
It never waits for a card, and never grants anything: what it proposes, a
person adds.

Only one job so far. What jobs share will be plain once there is a second.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Every job, and the settings each takes.
JOBS: dict[str, frozenset[str]] = {
    "runs-advice": frozenset({"model", "effort", "days", "prompt", "timeout"}),
}


@dataclass(frozen=True)
class Job:
    """One job's settings, with its defaults."""

    name: str
    #: Unset means the machine's `HALYARD_INSPECTION_MODEL`.
    model: str | None = None
    #: Unset means the machine's `HALYARD_INSPECTION_EFFORT`.
    effort: str | None = None
    #: How far back the log is read.
    days: int = 14
    #: A prompt of one's own in place of the one that ships with the job.
    prompt: Path | None = None
    #: How long the model may take before the job gives up and says so.
    timeout: float = 600.0


def _positive(job: str, key: str, value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"`upkeep: {job}: {key}:` must be a number above zero")
    return value


def from_yaml(text: str, *, base: Path | None = None) -> dict[str, Job]:
    """The `upkeep:` block, by job. `base` is what a relative `prompt:` is read
    from — the configuration's own directory."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None
    block = loaded.get("upkeep") if isinstance(loaded, dict) else None
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ValueError("`upkeep:` must be a mapping of job name to its settings")
    jobs: dict[str, Job] = {}
    for name, body in block.items():
        job = str(name)
        if job not in JOBS:
            raise ValueError(f"`upkeep: {job}:` is not a job. Known: {', '.join(sorted(JOBS))}")
        body = body or {}
        if not isinstance(body, dict):
            raise ValueError(f"`upkeep: {job}:` must be a mapping of settings")
        unknown = sorted(set(body) - JOBS[job])
        if unknown:
            raise ValueError(f"`upkeep: {job}:` has unknown setting(s) {', '.join(unknown)}")
        prompt = body.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError(f"`upkeep: {job}: prompt:` must be a path")
        where = Path(prompt).expanduser() if prompt else None
        if where is not None and not where.is_absolute() and base is not None:
            where = base / where
        jobs[job] = Job(
            name=job,
            model=str(body["model"]) if body.get("model") else None,
            effort=str(body["effort"]) if body.get("effort") else None,
            days=int(_positive(job, "days", body.get("days"), 14)),
            prompt=where,
            timeout=float(_positive(job, "timeout", body.get("timeout"), 600.0)),
        )
    return jobs


def load(name: str, directory: Path | None = None) -> Job:
    """One job's settings from the configuration, or its defaults."""
    from halyard.core.config_file import find_config

    path = find_config(directory)
    if path is None:
        return Job(name)
    try:
        jobs = from_yaml(path.read_text(encoding="utf-8"), base=path.parent)
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
    return jobs.get(name, Job(name))
