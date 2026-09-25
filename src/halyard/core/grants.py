"""Whether a call goes through without a card — decided in one place.

The service asks this for every call, and so does anything that wants to know
what the service would do: `halyard upkeep runs-advice` asks it again of the
cards the audit log kept, with today's rules. Two engines would drift, and an
analysis that answered by other rules than the gate would be advice about a
gate that does not exist.

Pure: nothing is recorded and nothing awaited. The service writes a grant down
before it honours it, and a grant it cannot write down is a card instead — see
`ApprovalService`.

Three grants, each for one kind of call and blind to the others: a name under
`tools:`, a shell command understood whole inside the project (`reads.py`), a
destination under `writes:`. At most one can apply to any call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from halyard.core import reads, tools, writes
from halyard.core.events import RiskLevel


@dataclass(frozen=True)
class Rules:
    """What a machine is configured to let through without a card."""

    #: `tools:` in `halyard.yaml`.
    tools: tuple[str, ...] = ()
    #: `writes:` in `halyard.yaml`.
    writes: tuple[str, ...] = ()
    #: `HALYARD_ALLOW_RISK_AT_OR_BELOW: low` — reads, and each project's
    #: trusted commands, only with it.
    reads: bool = False


@dataclass(frozen=True)
class Grant:
    """One reason a call goes through, and what to write down about it."""

    #: `tools`, `reads` or `writes`.
    kind: str
    #: What the runtime is told.
    reason: str
    #: `tools`: the pattern that named it.
    pattern: str = ""
    #: `reads`: why `reads.judge` let it through.
    why: str = ""
    #: `writes`: every file, and the pattern each matched.
    paths: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


def shell_judged(tool: str, rules: Rules) -> bool:
    """Whether a shell command is judged at all — so the caller knows whether
    to fetch a project's trusted commands before asking."""
    return rules.reads and tool in reads.SHELL_TOOLS


def grant_for(
    *,
    tool: str,
    command: str,
    risk: RiskLevel,
    cwd: str | None,
    project_dir: str | None,
    rules: Rules,
    runs: Sequence[reads.Run] = (),
    file_path: str | None = None,
    file_paths: Sequence[str] | None = None,
) -> Grant | None:
    """The grant this call has, or None — a card.

    `command` is the command as it will run, not its redacted copy. `risk` is
    the label `policy.classify` gave it: it decides nothing on its own, but a
    high one keeps a shell command a question however plain it looks. `runs`
    is what the project the call is in trusts, from its `runs:` and the table.
    """
    by_name = tools.allowed_by(tool, rules.tools)
    if by_name is not None:
        return Grant(
            "tools",
            f"Allowed without asking: {tool} matches {by_name!r} under `tools:`.",
            pattern=by_name,
        )
    if shell_judged(tool, rules) and risk is not RiskLevel.HIGH:
        verdict = reads.judge(command, cwd=cwd, project=project_dir or cwd, runs=runs)
        if verdict.allowed:
            return Grant("reads", f"Allowed without asking: {verdict.why}.", why=verdict.why)
    if tool in writes.FILE_TOOLS:
        paths = tuple(file_paths or ([file_path] if file_path else []))
        granted = writes.allowed_all(paths, project_dir or cwd, rules.writes)
        if granted is not None:
            matched = ", ".join(
                f"{path} matches {pattern!r}" for path, pattern in zip(paths, granted, strict=True)
            )
            return Grant(
                "writes",
                f"Allowed without asking: {matched} under `writes:` in halyard.yaml.",
                paths=paths,
                patterns=granted,
            )
    return None
