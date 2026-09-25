"""`runs-advice`: what a project could trust to run without a card, from the
cards it still asks.

Halyard holds the rod and a model fishes. Halyard gathers the evidence — which
shell commands its log says still came to a phone, how each ended, and what
today's rules would make of each — and a model reads it and proposes entries
for the project's trusted list. Every count is Halyard's. The model is asked
for proposals and reasons only; each proposal is checked the way an entry is,
and shown with the line that would add it, quoted by Halyard rather than
copied from the model.

**Judged again by the service's own rules.** Whether a card would still come
today is `grants.grant_for`, with this machine's settings and the project's
trusted commands: the question the service asks, not an estimate of it.

**Three answers, not two.** The log keeps each command redacted, so one that
redaction changed cannot be judged again and is counted as undetermined rather
than guessed. A card recorded before 2026-09-25 does not say where it ran; it
is judged from the project's root and counted as approximate.

**Read-only.** The database is opened with `mode=ro`, read in one go, and
closed before any model is asked, so a long turn holds nothing open. What the
turn used is recorded afterwards, by the runner, as every turn's is.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sqlite3
import urllib.parse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from halyard.core import grants, reads, refusals, trusted_runs
from halyard.core.config_file import Project
from halyard.core.policy import Policy
from halyard.core.redaction import MASK

#: The prompt that ships with the job. `upkeep: runs-advice: prompt:` replaces it.
PROMPT = Path(__file__).with_name("runs_advice.md")

#: How many families of cards the model is shown, and examples of each. The
#: rest are counted, so what was left out is said rather than lost.
FAMILIES = 40
EXAMPLES = 2
EXAMPLE_LENGTH = 200

#: Commands whose first word says little on its own: `git log` and `git push`
#: are different families.
_WITH_SUBCOMMAND = frozenset(
    {"git", "uv", "npm", "pnpm", "yarn", "npx", "make", "docker", "cargo", "go", "poetry", "gh"}
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Card:
    """One shell command that came to a phone, and what today would make of it."""

    command: str
    family: str
    #: `allowed`, `denied`, `timed out`, `unanswered` or `other`.
    outcome: str
    #: `passes`, `card`, `refused` or `undetermined`.
    today: str
    #: Judged from the project's root, the record not saying where it ran.
    approximate: bool
    tool: str
    cwd: str
    project_dir: str


@dataclass(frozen=True)
class Evidence:
    project: str
    path: str
    days: int
    since: datetime
    cards: tuple[Card, ...]
    #: Cards that were not shell commands — file writes, MCP calls. Not this
    #: job's to advise on, and counted so their absence is not a surprise.
    others: int
    #: What the project trusts now, and where each is written.
    trusted: tuple[tuple[str, str], ...]
    rules: grants.Rules
    runs: tuple[reads.Run, ...]


@dataclass(frozen=True)
class Proposal:
    entry: str
    why: str
    #: `reads.run_entry`'s reason, when it is not an entry at all.
    refused: str | None
    #: Cards in the window this would have spared, and of those how many were
    #: denied, timed out or never answered — a reason for care, not a count.
    spared: int
    spared_uneasy: int
    already: bool


@dataclass(frozen=True)
class Advice:
    proposals: tuple[Proposal, ...]
    keep_asking: tuple[tuple[str, str], ...]


# --- the evidence --------------------------------------------------------------


def family_of(command: str) -> str:
    """What a command is, for grouping: `uv run pytest`, `make test-fast`, `git log`."""
    found = reads.parts(command)
    if found is None:
        first = command.split()
        return f"{first[0][:40]} (not parsed)" if first else "(empty)"
    for words in found:
        rest = list(words)
        while rest and _ASSIGNMENT.match(rest[0]):
            rest.pop(0)
        if not rest or rest[0] == "cd":
            continue
        head = os.path.basename(rest[0])
        named = [word for word in rest[1:] if not word.startswith("-")]
        if head in _WITH_SUBCOMMAND and named:
            if named[0] in ("run", "exec") and len(named) > 1:
                return f"{head} {named[0]} {os.path.basename(named[1])}"
            return f"{head} {named[0]}"
        return head
    return "cd"


def _outcome(resolved: dict[str, Any] | None) -> str:
    if resolved is None:
        return "unanswered"
    decision, reason = resolved.get("decision"), resolved.get("reason")
    if decision == "allow":
        return "allowed"
    if decision == "deny" and reason == "user":
        return "denied"
    if decision == "deny" and reason in ("timeout", "expired"):
        return "timed out"
    return "other"


def _read(database: Path, since: datetime, name: str) -> tuple[list, dict, tuple[str, ...]]:
    """Everything the job needs from the database, over a read-only connection
    that is closed before anything else happens."""
    if not database.is_file():
        return [], {}, ()
    uri = f"file:{urllib.parse.quote(str(database))}?mode=ro"
    requested: list[tuple[str, str, dict]] = []
    resolved: dict[str, dict] = {}
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            for request_id, project, detail in db.execute(
                "SELECT request_id, project, detail FROM audit_log "
                "WHERE action = 'approval.requested' AND recorded_at >= ? ORDER BY sequence",
                (since.isoformat(),),
            ):
                with contextlib.suppress(ValueError, TypeError):
                    requested.append((request_id, project, json.loads(detail)))
            for request_id, detail in db.execute(
                "SELECT request_id, detail FROM audit_log "
                "WHERE action = 'approval.resolved' AND recorded_at >= ?",
                (since.isoformat(),),
            ):
                with contextlib.suppress(ValueError, TypeError):
                    resolved[request_id] = json.loads(detail)
            kept = trusted_runs.read(db, name).get(name, ())
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return requested, resolved, ()
    return requested, resolved, kept


def _today(
    command: str,
    tool: str,
    cwd: str,
    project_dir: str,
    redacted: bool,
    rules: grants.Rules,
    runs: tuple[reads.Run, ...],
    refuse_commits: bool,
) -> str:
    if redacted or MASK in command:
        return "undetermined"
    if refusals.writes_history_if(command, refuse_commits):
        return "refused"
    risk = Policy().classify(command).risk
    grant = grants.grant_for(
        tool=tool,
        command=command,
        risk=risk,
        cwd=cwd,
        project_dir=project_dir,
        rules=rules,
        runs=runs,
    )
    return "passes" if grant is not None else "card"


def gather(
    database: Path,
    project: Project,
    rules: grants.Rules,
    *,
    days: int,
    refuse_commits: bool = False,
    now: datetime | None = None,
) -> Evidence:
    """What the log says came to a phone from this project in the last `days`,
    each judged again by today's rules."""
    if project.path is None:
        raise ValueError(f"project {project.name!r} has no `path:`")
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    root = os.path.realpath(os.path.expanduser(str(project.path)))
    requested, resolved, kept = _read(database, since, project.name)
    trusted = [(entry, "halyard.yaml") for entry in project.runs]
    trusted += [(entry, "halyard rules") for entry in kept if entry not in project.runs]
    runs = tuple(reads.run_entry(entry) for entry, _ in trusted)
    names = {project.name, os.path.basename(root)}
    cards: list[Card] = []
    others = 0
    for request_id, where, detail in requested:
        given = detail.get("project_dir") or ""
        if where not in names and os.path.realpath(given or "/") != root:
            continue
        tool = str(detail.get("tool") or "")
        if tool not in reads.SHELL_TOOLS:
            others += 1
            continue
        command = str(detail.get("command") or "")
        cwd, project_dir = detail.get("cwd"), detail.get("project_dir")
        approximate = not cwd and not project_dir
        cwd, project_dir = (root, root) if approximate else (cwd or project_dir, project_dir)
        cards.append(
            Card(
                command=command,
                family=family_of(command),
                outcome=_outcome(resolved.get(request_id)),
                today=_today(
                    command,
                    tool,
                    cwd,
                    project_dir,
                    bool(detail.get("redacted")),
                    rules,
                    runs,
                    refuse_commits,
                ),
                approximate=approximate,
                tool=tool,
                cwd=cwd,
                project_dir=project_dir or "",
            )
        )
    return Evidence(
        project=project.name,
        path=root,
        days=days,
        since=since,
        cards=tuple(cards),
        others=others,
        trusted=tuple(trusted),
        rules=rules,
        runs=runs,
    )


def _counts(cards: list[Card]) -> Counter:
    return Counter(card.outcome for card in cards)


def render(evidence: Evidence, *, most: int = FAMILIES) -> str:
    """The evidence as the model is given it, and as `--evidence` prints it."""
    cards = list(evidence.cards)
    outcomes = _counts(cards)
    today = Counter(card.today for card in cards)
    lines = [
        f"Project: {evidence.project} ({evidence.path})",
        f"Window: the last {evidence.days} days, since {evidence.since:%Y-%m-%d %H:%M} UTC.",
        f"Shell commands that came as cards: {len(cards)} — allowed {outcomes['allowed']}, "
        f"denied {outcomes['denied']}, timed out {outcomes['timed out']}, "
        f"unanswered {outcomes['unanswered']}, other {outcomes['other']}.",
        f"Judged again by today's rules: {today['passes']} would pass now, {today['card']} "
        f"would still be cards, {today['refused']} would be refused outright, "
        f"{today['undetermined']} cannot be told from the record (redacted).",
        f"Judged from the project's root because the record did not say where they ran "
        f"(approximate): {sum(card.approximate for card in cards)}.",
        f"Other cards, not shell commands and not part of this: {evidence.others}.",
    ]
    if not evidence.rules.reads:
        lines.append(
            "HALYARD_ALLOW_RISK_AT_OR_BELOW is off on this machine: nothing passes without a "
            "card until it is on, whatever the project trusts."
        )
    lines += ["", "Trusted now:"]
    lines += [f"  - {entry}  ({where})" for entry, where in evidence.trusted] or ["  (nothing)"]

    still = [card for card in cards if card.today == "card"]
    families: dict[str, list[Card]] = {}
    for card in still:
        families.setdefault(card.family, []).append(card)
    ranked = sorted(families.items(), key=lambda item: (-len(item[1]), item[0]))
    shown, left = ranked[:most], ranked[most:]
    lines += [
        "",
        f"Still cards today, by family — {len(shown)} of {len(ranked)} families shown"
        + (
            f"; {len(left)} families with {sum(len(c) for _, c in left)} cards left out:"
            if left
            else ":"
        ),
    ]
    for family, members in shown:
        counted = _counts(members)
        lines.append(
            f"- {family}: {len(members)} cards (allowed {counted['allowed']}, denied "
            f"{counted['denied']}, timed out {counted['timed out']}, unanswered "
            f"{counted['unanswered']}; approximate {sum(c.approximate for c in members)})"
        )
        examples = list(dict.fromkeys(card.command for card in members))[:EXAMPLES]
        for example in examples:
            cut = example[:EXAMPLE_LENGTH] + ("…" if len(example) > EXAMPLE_LENGTH else "")
            lines.append(f"    e.g. {cut!r}")
    unknown = Counter(card.family for card in cards if card.today == "undetermined")
    if unknown:
        lines += ["", "Cannot be told from the record, by family:"]
        lines += [f"- {family}: {count}" for family, count in unknown.most_common(10)]
    return "\n".join(lines) + "\n"


# --- the answer ------------------------------------------------------------------


def _json(answer: str) -> dict | None:
    """The object the model was asked for, wherever in its answer it put it."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        found = json.loads(answer[start : end + 1])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def advise(answer: str, evidence: Evidence) -> Advice | None:
    """The model's proposals, each checked and counted by Halyard. None when
    the answer is not in the shape it was asked for."""
    found = _json(answer)
    if found is None or not isinstance(found.get("proposals", []), list):
        return None
    still = [card for card in evidence.cards if card.today == "card"]
    # What an entry would do once reads are on — the setting being off would
    # otherwise make every proposal look useless.
    rules = replace(evidence.rules, reads=True)
    trusted = {entry for entry, _ in evidence.trusted}
    proposals: list[Proposal] = []
    for item in found.get("proposals", []):
        if not isinstance(item, dict) or not isinstance(item.get("entry"), str):
            continue
        entry, why = item["entry"].strip(), str(item.get("why") or "").strip()
        try:
            run = reads.run_entry(entry)
        except ValueError as refused:
            proposals.append(Proposal(entry, why, str(refused), 0, 0, False))
            continue
        spared = [
            card
            for card in still
            if grants.grant_for(
                tool=card.tool,
                command=card.command,
                risk=Policy().classify(card.command).risk,
                cwd=card.cwd,
                project_dir=card.project_dir,
                rules=rules,
                runs=(*evidence.runs, run),
            )
            is not None
        ]
        uneasy = sum(card.outcome in ("denied", "timed out", "unanswered") for card in spared)
        proposals.append(Proposal(entry, why, None, len(spared), uneasy, entry in trusted))
    keep = tuple(
        (str(item.get("family") or "").strip(), str(item.get("why") or "").strip())
        for item in found.get("keep_asking", []) or []
        if isinstance(item, dict) and item.get("family")
    )
    return Advice(tuple(proposals), keep)


def render_advice(evidence: Evidence, advice: Advice) -> str:
    """What the person reads: each proposal with Halyard's counts and the line
    that adds it, then what should stay a question."""
    name = shlex.quote(evidence.project)
    lines = [
        f"Proposals for {evidence.project} — checked by Halyard; the counts are from its log "
        f"of the last {evidence.days} days, not from the model.",
    ]
    if not evidence.rules.reads:
        lines.append(
            "HALYARD_ALLOW_RISK_AT_OR_BELOW is off here, so none of these applies until it is on."
        )
    for proposal in advice.proposals:
        lines += ["", f"  {proposal.entry}"]
        if proposal.refused:
            lines.append(f"    not an entry: {proposal.refused}")
        elif proposal.already:
            lines.append("    already trusted")
        else:
            spared = (
                f"would have spared {proposal.spared} of the cards"
                if proposal.spared
                else "would have spared none of the cards"
            )
            if proposal.spared_uneasy:
                spared += (
                    f" — {proposal.spared_uneasy} of them were denied, timed out or never answered"
                )
            lines.append(f"    {spared}")
        if proposal.why:
            lines.append(f"    why: {proposal.why}")
        if not proposal.refused and not proposal.already:
            lines.append(f"    halyard rules add {name} {shlex.quote(proposal.entry)}")
    if not advice.proposals:
        lines += ["", "  (no proposals)"]
    if advice.keep_asking:
        lines += ["", "Keep asking:"]
        for family, why in advice.keep_asking:
            lines.append(f"  {family} — {why}" if why else f"  {family}")
    return "\n".join(lines) + "\n"
