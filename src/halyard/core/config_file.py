"""Seats from a YAML file, arranged by project.

The environment dialect works and stays. It stops being readable at about four
seats, though, and it cannot say which project a seat belongs to at all — one
flat list, one project, and a `HALYARD_SEAT_XDRV=runtime=codex session=…` line
that has to be parsed by eye.

    projects:
      alpha-engine:
        path: ~/code/alpha-engine
        seats:
          nav:
            runtime: claude-code
            session: alpha-navigator
            chat: "-1001"
            role: navigator
          xdrv:
            runtime: codex
            session: alpha-xdriver
            chat: "-1004"

The hierarchy is the point: a project, its path, and the seats sitting in it.
Adding a second project is adding a second block rather than inventing a naming
convention inside a flat namespace.

**This produces exactly what the environment produces.** Everything downstream —
routing, `doctor`, the wizard's defaults — is written against a list of `Seat`,
and a second producer that yielded something subtly different would be the
shape both Codex postmortems warn about. `tests/test_seat_contract.py` holds
both producers to one set of cases for that reason.

Chat ids are quoted in the examples deliberately: `-1001` is a number to YAML
and a string everywhere else, and an unquoted one arrives as `-1001` the int.
That is handled here rather than left to whoever writes the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from halyard.core.events import Role
from halyard.core.seats import Seat, _default_runtime, known_runtimes

#: Where a project's own settings live, beside its seats.
_PROJECT_FIELDS = {
    "path",
    "seats",
    "name",
    "validate",
    "warn_if",
    "commands",
    "forge",
    "labels",
    "label_groups",
    "label_findings",
    "label_work",
    "confirmation",
    "inspections",
    # The name `inspections:` had until 2026-09-23, still read.
    "checks",
    "handoffs",
    "workflows",
}
_SEAT_FIELDS = {
    "runtime",
    "session",
    "chat",
    "role",
    "after_compaction",
    "before_compaction",
    "task_label",
}


@dataclass(frozen=True)
class Confirmation:
    """An extra round before a commit, for what a guard cannot catch.

    Two files, both belonging to the project rather than to Halyard, because
    what is worth asking again is a thing a team learns about itself. Paths are
    read relative to the project.

    `inquiry` is put in front of the model while it writes the commit message,
    and asks it for one more judgement: is this change worth a round? `review`
    is what that round consists of, and goes to the navigator when somebody
    presses the button.
    """

    #: What the model is asked, on top of writing the message.
    inquiry: Path | None = None
    #: What the navigator is sent when the round is asked for.
    review: Path | None = None


@dataclass(frozen=True)
class Handoff:
    """One way a reply goes from one seat to another, as a project defines it.

    A button on `/handoff`'s card. It carries the chat's last reply, puts the
    project's own text in front of it, runs whichever of the project's inspections
    it names over the reply first, and delivers the lot to a seat — the one `to:`
    names, or whichever is pressed. Everything it reads belongs to the project;
    Halyard adds only what it can see for itself. See `halyard.handoffs`.
    """

    name: str
    #: The project's own text for whoever receives it — `review.md`. Read
    #: relative to the project. Optional: a handoff can be the reply alone.
    prompt: Path | None = None
    #: The project's text for every round after the first — each time this
    #: handoff goes again for the same work item — `review-followup.md`. Unset
    #: sends `prompt` every round. See `halyard.handoffs.rounds`.
    followup_prompt: Path | None = None
    #: Whether the chat's last reply goes with it. Almost always.
    include_last_message: bool = True
    #: Inspections from this project's `inspections:`, run over the reply before
    #: it goes — written `inspect:` on the handoff.
    inspections: tuple[str, ...] = ()
    #: Commands from this project's `commands:`, run one after another before
    #: the inspections. What each did goes into the envelope the inspections
    #: read, and into the message; a failure is reported, not a reason to stop.
    commands: tuple[str, ...] = ()
    #: A role (`navigator`) or a seat's label. Unset offers every seat.
    to: str | None = None


#: How many rounds a step may have before a run stops and asks. A step goes once
#: unless it says otherwise; one the work is meant to come back to says how
#: often, so going round is something a project wrote down rather than assumed.
_DEFAULT_ROUNDS = 1


@dataclass(frozen=True)
class Step:
    """One step of a workflow: a handoff, the seat it goes to, how often it may go.

    Named once, under `workflows: steps:`, because every flow uses the same
    ones — and one handoff going to two different drivers is two steps, which
    is where a Codex driver and a ZCode one are told apart.
    """

    name: str
    #: One of the project's `handoffs:`. The step's own name when it says none.
    handoff: str
    #: A seat's label or a role. Unset leaves it to the handoff's `to:`, which
    #: is enough until one role is held by two seats.
    seat: str | None = None
    #: How many rounds this step may have in one run — and, inside a flow's
    #: phases, in each phase: the second phase's `discover` starts from one.
    rounds: int = _DEFAULT_ROUNDS
    #: Another step, whose decision this one acts on when its own reply decides
    #: nothing — `reviewed` after `review`, so a reviewer's back reaches the
    #: navigator first and the navigator's reply goes back to the reviewer.
    decided_by: str | None = None


@dataclass(frozen=True)
class Decisions:
    """The words a reply's last line decides in.

    Each is its own name unless a project says otherwise — `forward`, `back`,
    `wait`, `next` — so a project whose prompts ask for those writes nothing,
    and one whose prompts ask for other words names them: `decisions: {forward:
    go}`. `next` only means something at the end of a flow's phases.
    """

    forward: str = "forward"
    back: str = "back"
    wait: str = "wait"
    next: str = "next"


#: How many phases a flow may go through before a run stops and asks. Past it
#: the next phase is the operator's to send, as a round past `rounds:` is.
_DEFAULT_PHASES = 3


@dataclass(frozen=True)
class Workflows:
    """A project's workflows: the steps they share, the words that decide, and
    each flow as the order it takes its steps in. See `halyard.workflows`."""

    steps: dict[str, Step] = field(default_factory=dict)
    decisions: Decisions = field(default_factory=Decisions)
    #: Each flow by name, as the step names it takes in order. `steps`,
    #: `decisions` and `phases` are not flows, which is why a workflow cannot
    #: be called any of them.
    flows: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: The stretch of a flow that goes round once per phase — written as a list
    #: inside the flow — as the places of its first and last steps. Only flows
    #: that have one are here.
    stretches: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: How many phases any stretch may go through before a run stops and asks.
    phases: int = _DEFAULT_PHASES


@dataclass(frozen=True)
class Project:
    """A codebase, its location, and the seats working in it."""

    name: str
    #: Where to wire the gate. Optional: a project can be described before
    #: anybody decides to gate it.
    path: Path | None
    seats: list[Seat]
    #: What has to pass before `/review_and_commit` offers a commit: the name
    #: of one of `commands:` — `test-fast` — so that everything Halyard runs for
    #: a project is in that one list. Optional, and absent means no check runs
    #: rather than some guessed default: a command invented for somebody's
    #: repository would fail on every commit.
    validate: str | None = None
    #: Which of the named warnings apply here. `None` means the default set;
    #: an empty list means none, which is how somebody who does not share this
    #: project's conventions turns them all off. See `commits.validation`.
    warn_if: tuple[str, ...] | None = None
    #: Named commands this project offers to `/command` — `test-all: make
    #: test-all`. Empty by default: these run whatever they are given on the
    #: machine the control plane is on, so the list is what somebody wrote down
    #: and never a guess about what a project probably supports.
    commands: dict[str, str] = field(default_factory=dict)
    #: Commands that run other commands, in order, stopping at the first that
    #: fails — `next-task: [cleanup, pull-branch]`, written under `commands:`
    #: like the rest and offered by `/command` with them.
    command_lists: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Which kind of issue tracker this project's remote points at. Only needed
    #: for a host that does not name itself — `gitlab.com` does, and
    #: `git.example.com` cannot.
    forge: str | None = None
    #: Which labels `/label` offers. Empty means every label the project has,
    #: which is the right default until a project has more of them than a phone
    #: keyboard can show.
    labels: tuple[str, ...] = ()
    #: Groups of task labels, by name — `level: [level::1, level::2, level::3]`.
    #: The first label a task carries from each goes on the envelope that
    #: inspections and handoffs are given, as `level: level::3`. Empty unless
    #: configured. A task with none of a group's labels, or a tracker that
    #: cannot be read, adds nothing: this reports what is there, it does not ask
    #: for anything.
    label_groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: What this project's inspections answer when they found something, in its
    #: own words — `status: candidate`. An answer that says one of them puts
    #: `halyard:<inspection>` on the task, wherever it ran. Empty unless
    #: configured, and nothing is written without it: this writes to somebody's
    #: tracker on its own.
    label_findings: tuple[str, ...] = ()
    #: Whether each seat's label goes on the task its branch is for, the first
    #: time that seat works on it — `navigator:claude`. Off unless asked for:
    #: it writes to somebody's issue tracker on its own. See `tasks.attribution`.
    label_work: bool = False
    #: The extra round this project asks for before closing a piece of work.
    #: `None` means no such round exists here, and `/commit` is unchanged.
    confirmation: Confirmation | None = None
    #: What `/inspect` runs over the last reply in a chat, by name — `proof:
    #: NOTES/inspections/proof.md`. Each is the project's own file, read relative
    #: to the project and put in front of a model on its own. Empty unless
    #: configured. See `halyard.inspections`.
    inspections: dict[str, Path] = field(default_factory=dict)
    #: How a reply is handed from one seat to another here, by name — see
    #: `Handoff`. Empty unless configured.
    handoffs: dict[str, Handoff] = field(default_factory=dict)
    #: The spellings this project's configuration still uses from before they
    #: were renamed, as a sentence each, for `doctor` to mention. They work.
    older: tuple[str, ...] = ()
    #: The flows this project takes its handoffs in, and the steps they share
    #: — see `Workflows`. Empty unless configured.
    workflows: Workflows = field(default_factory=Workflows)


def _confirmation_from(project: str, value: Any) -> Confirmation | None:
    """`confirmation:` as two paths, or None when the block is absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Project {project!r}: `confirmation:` must be a mapping.")
    unknown = set(value) - {"inquiry", "review"}
    if unknown:
        raise ValueError(
            f"Project {project!r}: `confirmation:` has unknown field(s) "
            f"{', '.join(sorted(unknown))}"
        )
    inquiry = _as_text(value.get("inquiry"))
    review = _as_text(value.get("review"))
    return Confirmation(
        inquiry=Path(inquiry).expanduser() if inquiry else None,
        review=Path(review).expanduser() if review else None,
    )


def _spelled(project: str, body: dict, new: str, old: str, where: str) -> tuple[Any, str | None]:
    """The value under `new`, or under the `old` name it had before — and, when
    it was the old one, a sentence saying so. Both at once is refused: which of
    the two was meant is not something to guess."""
    if new in body and old in body:
        raise ValueError(
            f"Project {project!r}: {where} has both `{new}:` and `{old}:` — "
            f"they are the same thing; keep `{new}:`."
        )
    if old in body:
        return body.get(old), f"{where}: `{old}:` is now `{new}:`"
    return body.get(new), None


def _inspections_from(project: str, value: Any) -> dict[str, Path]:
    """`inspections:` as a mapping of name to the file that says what to look for.

    Strict about the shape: an inspection written as a mapping would otherwise
    become a path spelled with its own braces, and fail only when somebody ran it.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Project {project!r}: `inspections:` must be a mapping of name to file.")
    found: dict[str, Path] = {}
    for name, where in value.items():
        if not str(name).strip() or not isinstance(where, str) or not where.strip():
            raise ValueError(
                f"Project {project!r}: inspection {name!r} needs a file, "
                "like `proof: NOTES/inspections/proof.md`."
            )
        found[str(name).strip()] = Path(where.strip()).expanduser()
    return found


#: A handoff's name rides in a button, where Telegram allows 64 bytes of
#: callback data, and is typed after `/handoff`.
_HANDOFF_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
_HANDOFF_FIELDS = {
    "prompt",
    "followup_prompt",
    "include_last_message",
    "inspect",
    # The name `inspect:` had until 2026-09-23, still read.
    "checks",
    "commands",
    "to",
}


def _handoffs_from(
    project: str,
    value: Any,
    *,
    inspections: dict[str, Path],
    seats: list[Seat],
    commands: dict[str, str] | None = None,
    older: list[str] | None = None,
) -> dict[str, Handoff]:
    """`handoffs:` as a mapping of name to how that handoff is made.

    Checked against the rest of the project here, because a handoff naming a
    check, a command or a seat nobody defined would otherwise fail only when
    somebody pressed it — from a phone, in the middle of a piece of work.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Project {project!r}: `handoffs:` must be a mapping of name to handoff.")
    roles = {role.value for role in Role}
    labels = {seat.label for seat in seats}
    found: dict[str, Handoff] = {}
    for raw, spec in value.items():
        name = str(raw).strip()
        where = f"Project {project!r}: handoff {name!r}"
        if not _HANDOFF_NAME.match(name):
            raise ValueError(
                f"{where} needs a name of lowercase letters, digits, `-` or `_`, "
                "up to 32 characters."
            )
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"{where} must be a mapping.")
        unknown = set(spec) - _HANDOFF_FIELDS
        if unknown:
            raise ValueError(f"{where} has unknown field(s) {', '.join(sorted(unknown))}")
        named, spelled = _spelled(project, spec, "inspect", "checks", f"handoff {name!r}")
        if spelled is not None and older is not None:
            older.append(spelled)
        named = named or []
        if not isinstance(named, list) or not all(isinstance(n, str) and n.strip() for n in named):
            raise ValueError(f"{where}: `inspect:` must be a list of inspection names.")
        named = [n.strip() for n in named]
        if missing := [n for n in named if n not in inspections]:
            raise ValueError(
                f"{where} names inspections this project does not define: {', '.join(missing)}"
            )
        ran = spec.get("commands") or []
        if not isinstance(ran, list) or not all(isinstance(n, str) and n.strip() for n in ran):
            raise ValueError(f"{where}: `commands:` must be a list of command names.")
        ran = [n.strip() for n in ran]
        if missing := [n for n in ran if n not in (commands or {})]:
            raise ValueError(
                f"{where} names commands this project does not define: {', '.join(missing)}"
            )
        carries = spec.get("include_last_message")
        carries = (
            True if carries is None else _as_flag(project, f"{name}.include_last_message", carries)
        )
        if named and not carries:
            raise ValueError(
                f"{where} runs inspections over the last message, so it has to carry it."
            )
        prompt = _as_text(spec.get("prompt"))
        followup = _as_text(spec.get("followup_prompt"))
        if not prompt and not carries and not ran:
            raise ValueError(
                f"{where} hands on nothing: give it a `prompt:`, the last message or a command."
            )
        to = _as_text(spec.get("to"))
        if to and to.lower() not in roles and to not in labels:
            raise ValueError(
                f"{where}: `to:` must be a role ({', '.join(sorted(roles))}) "
                "or one of this project's seats."
            )
        found[name] = Handoff(
            name=name,
            prompt=Path(prompt).expanduser() if prompt else None,
            followup_prompt=Path(followup).expanduser() if followup else None,
            include_last_message=carries,
            inspections=tuple(named),
            commands=tuple(ran),
            to=to.lower() if to and to.lower() in roles else to,
        )
    return found


#: What a step may say, the words a decision can be given, and the names under
#: `workflows:` that are not flows.
_STEP_FIELDS = {"handoff", "seat", "rounds", "decided_by"}
_DECISION_FIELDS = ("forward", "back", "wait", "next")
_NOT_A_FLOW = ("steps", "decisions", "phases")


def _workflows_from(
    project: str,
    value: Any,
    *,
    handoffs: dict[str, Handoff],
    seats: list[Seat],
) -> Workflows:
    """`workflows:` — the steps every flow shares, the words that decide, and
    the flows themselves, each a list of step names in the order it takes them.

    Checked against the rest of the project here, the way a handoff is: a flow
    naming a step, a step naming a handoff, or a step naming a seat that nobody
    defined would otherwise fail when somebody started it from a phone.
    """
    if value is None:
        return Workflows()
    if not isinstance(value, dict):
        raise ValueError(
            f"Project {project!r}: `workflows:` must be a mapping — `steps:`, `decisions:`, "
            "and a list of step names for each workflow."
        )
    steps = _steps_from(project, value.get("steps"), handoffs=handoffs, seats=seats)
    flows: dict[str, tuple[str, ...]] = {}
    stretches: dict[str, tuple[int, int]] = {}
    for raw, listed in value.items():
        name = str(raw).strip()
        if name in _NOT_A_FLOW:
            continue
        where = f"Project {project!r}: workflow {name!r}"
        if not isinstance(listed, list) or not listed:
            raise ValueError(
                f"{where} must be a list of step names, in the order it takes them. "
                f"({', '.join(_NOT_A_FLOW)} are the names here that are not workflows.)"
            )
        wanted, stretch = _flow_from(where, listed)
        if missing := [step for step in wanted if step not in steps]:
            raise ValueError(
                f"{where} names steps this project does not define under "
                f"`workflows: steps:`: {', '.join(missing)}"
            )
        flows[name] = tuple(wanted)
        if stretch is not None:
            stretches[name] = stretch
    return Workflows(
        steps=steps,
        decisions=_decisions_from(project, value.get("decisions")),
        flows=flows,
        stretches=stretches,
        phases=_phases_from(project, value.get("phases")),
    )


def _flow_from(where: str, listed: list) -> tuple[list[str], tuple[int, int] | None]:
    """A flow's step names in order, and where its phases are, if it has any.

    One list inside the flow is the stretch that goes round once per phase —
    `[to_nav, review, [discover, develop, verified], close]` — so the steps
    before it happen once, and so do the ones after it. One stretch at most:
    a second would be a second phase count, and nobody reading a card could
    tell which one it meant.
    """
    wanted: list[str] = []
    stretch: tuple[int, int] | None = None
    for entry in listed:
        if not isinstance(entry, list):
            wanted.append(str(entry).strip())
            continue
        if stretch is not None:
            raise ValueError(f"{where} has two lists of phases in it; a workflow has one.")
        if not entry or any(isinstance(inner, (list, dict)) for inner in entry):
            raise ValueError(
                f"{where}: its phases must be a list of step names, and not an empty one."
            )
        stretch = (len(wanted), len(wanted) + len(entry) - 1)
        wanted.extend(str(inner).strip() for inner in entry)
    return wanted, stretch


def _phases_from(project: str, value: Any) -> int:
    """`workflows: phases:` — how many phases a run may go through unasked."""
    if value is None:
        return _DEFAULT_PHASES
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"Project {project!r}: `workflows: phases:` must be a whole number of at least 1."
        )
    return value


def _decisions_from(project: str, value: Any) -> Decisions:
    """`workflows: decisions:` — the words to read instead of `forward`, `back`
    and `wait`. Any left out keep their own name."""
    if value is None:
        return Decisions()
    where = f"Project {project!r}: `workflows: decisions:`"
    if not isinstance(value, dict):
        raise ValueError(
            f"{where} must be a mapping of {', '.join(_DECISION_FIELDS)} to the word a reply "
            "ends with."
        )
    if unknown := set(value) - set(_DECISION_FIELDS):
        raise ValueError(f"{where} has unknown field(s) {', '.join(sorted(unknown))}")
    words = {key: _as_text(value.get(key)) or key for key in _DECISION_FIELDS}
    if any(":" in word for word in words.values()):
        # The word is read after the last colon on the line, so one holding a
        # colon could never be read at all.
        raise ValueError(f"{where}: a word cannot contain `:`.")
    if len({word.casefold() for word in words.values()}) < len(words):
        raise ValueError(f"{where}: two decisions cannot share a word.")
    return Decisions(**words)


def _steps_from(
    project: str,
    value: Any,
    *,
    handoffs: dict[str, Handoff],
    seats: list[Seat],
) -> dict[str, Step]:
    """`workflows: steps:` as a mapping of name to what that step does."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"Project {project!r}: `workflows: steps:` must be a mapping of name to step."
        )
    roles = {role.value for role in Role}
    labels = {seat.label for seat in seats}
    found: dict[str, Step] = {}
    for raw, spec in value.items():
        name = str(raw).strip()
        where = f"Project {project!r}: step {name!r}"
        if not _HANDOFF_NAME.match(name):
            raise ValueError(
                f"{where} needs a name of lowercase letters, digits, `-` or `_`, "
                "up to 32 characters."
            )
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"{where} must be a mapping.")
        unknown = set(spec) - _STEP_FIELDS
        if unknown:
            raise ValueError(f"{where} has unknown field(s) {', '.join(sorted(unknown))}")
        handoff = _as_text(spec.get("handoff")) or name
        if handoff not in handoffs:
            raise ValueError(
                f"{where} names the handoff {handoff!r}, which this project does not define."
            )
        seat = _as_text(spec.get("seat"))
        if seat and seat.lower() not in roles and seat not in labels:
            raise ValueError(
                f"{where}: `seat:` must be a role ({', '.join(sorted(roles))}) "
                "or one of this project's seats."
            )
        rounds = spec.get("rounds", _DEFAULT_ROUNDS)
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
            raise ValueError(f"{where}: `rounds:` must be a whole number of at least 1.")
        found[name] = Step(
            name=name,
            handoff=handoff,
            seat=seat.lower() if seat and seat.lower() in roles else seat,
            rounds=rounds,
            decided_by=_as_text(spec.get("decided_by")),
        )
    for step in found.values():
        # Checked once every step is known: the one it names may come later.
        if step.decided_by is not None and (
            step.decided_by == step.name or step.decided_by not in found
        ):
            raise ValueError(
                f"Project {project!r}: step {step.name!r}: `decided_by:` must name another "
                "step under `workflows: steps:`."
            )
    return found


def _commands_from(project: str, value: Any) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """`commands:` as command lines by name, and the lists of them by name.

    A command is a line — `cleanup: make cleanup` — or the names of other
    commands to run in order: `next-task: [cleanup, pull-branch]`. A list names
    lines only, so what runs is always something written down as a line.
    """
    if value is None:
        return {}, {}
    if not isinstance(value, dict):
        raise ValueError(f"Project {project!r}: `commands:` must be a mapping of name to command.")
    lines: dict[str, str] = {}
    lists: dict[str, tuple[str, ...]] = {}
    for raw, spec in value.items():
        if isinstance(spec, list):
            lists[str(raw)] = tuple(str(name).strip() for name in spec if str(name).strip())
        else:
            lines[str(raw)] = str(spec)
    for name, listed in lists.items():
        where = f"Project {project!r}: command {name!r}"
        if not listed:
            raise ValueError(f"{where} is an empty list — name the commands it runs, in order.")
        if nested := [entry for entry in listed if entry in lists]:
            raise ValueError(
                f"{where} runs {', '.join(nested)}, which is a list itself — name the "
                "commands in it instead."
            )
        if missing := [entry for entry in listed if entry not in lines]:
            raise ValueError(
                f"{where} runs commands this project does not define: {', '.join(missing)}"
            )
    return lines, lists


def _not_a_list(project: str, body: dict, command_lists: dict[str, tuple[str, ...]]) -> None:
    """A handoff or `validate:` names commands themselves, never a list of them.

    Said before either is read, which would otherwise call a list's name a
    command this project does not define — true, and no help.
    """
    handoffs = body.get("handoffs")
    for handoff, spec in (handoffs if isinstance(handoffs, dict) else {}).items():
        ran = spec.get("commands") if isinstance(spec, dict) else None
        for name in ran if isinstance(ran, list) else []:
            if str(name).strip() in command_lists:
                raise ValueError(
                    f"Project {project!r}: handoff {str(handoff)!r} names {str(name).strip()!r}, "
                    "which is a list of commands — name the commands in it instead."
                )
    if (validate := _as_text(body.get("validate"))) in command_lists:
        raise ValueError(
            f"Project {project!r}: `validate:` names {validate!r}, which is a list of "
            "commands — `validate:` names one command."
        )


def _placeholders_checked(
    project: str,
    commands: dict[str, str],
    label_groups: dict[str, tuple[str, ...]],
    *,
    validate: str | None,
    handoffs: dict[str, Handoff],
) -> None:
    """What a command line fills in is something that can be filled in.

    `{label_groups.<group>}` names a group this project has, with labels in
    it. `{input.<name>}` is typed by somebody, so only `/command` can run it: a
    handoff has nobody to ask, and neither does a commit's `validate:`, which
    takes no label either. Refused here rather than when the command runs,
    where the line would reach the shell with its braces still in it. See
    `halyard.commands.labels` and `halyard.commands.inputs`.
    """
    from halyard.commands.inputs import names_in
    from halyard.commands.labels import groups_in

    for name, line in commands.items():
        for group in groups_in(line):
            where = f"Project {project!r}: command {name!r} takes {{label_groups.{group}}}"
            if group not in label_groups:
                defined = f" ({', '.join(label_groups)})" if label_groups else ""
                raise ValueError(f"{where}, but `label_groups:` has no group {group!r}{defined}.")
            if not label_groups[group]:
                raise ValueError(f"{where}, and that group lists no labels to take one from.")
        if name == validate and (groups_in(line) or names_in(line)):
            raise ValueError(
                f"Project {project!r}: `validate:` names {name!r}, which takes a value — a "
                "task's label or something typed — and a commit has nowhere to ask for one. "
                "Give `validate:` a command of its own."
            )
    for handoff in handoffs.values():
        for name in handoff.commands:
            if typed := names_in(commands.get(name, "")):
                raise ValueError(
                    f"Project {project!r}: handoff {handoff.name!r} runs {name!r}, which takes "
                    f"{{input.{typed[0]}}} — typed by somebody, and a handoff has nobody to "
                    "ask. Run it with /command."
                )


def _validate_from(project: str, value: Any, commands: dict[str, str]) -> str | None:
    """`validate:` as the name of one of the project's `commands:`.

    Not a command line of its own: what Halyard runs for a project is what
    `commands:` lists, and a line written anywhere else is one nobody reading
    that list can see. Checked here, as a handoff's commands are, rather than
    when somebody presses the button.
    """
    name = _as_text(value)
    if name is None or name in commands:
        return name
    listed = f" ({', '.join(commands)})" if commands else ""
    raise ValueError(
        f"Project {project!r}: `validate:` names {name!r}, which is not one of its "
        f"`commands:`{listed}. Write the command there and its name here — "
        "`validate: test-fast`."
    )


def _warnings_from(project: str, value: Any) -> tuple[str, ...] | None:
    """`warn_if:` as a tuple, or None when it was not written at all.

    None and `[]` mean different things and both are wanted: unwritten takes
    the default set, empty turns every warning off.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"Project {project!r}: `warn_if:` must be a list of names.")
    return tuple(str(name).strip() for name in value if str(name).strip())


def _findings_from(project: str, value: Any) -> tuple[str, ...]:
    """`label_findings:` as phrases, in the project's own words.

    A phrase is nearly always `status: something`, which YAML reads as a
    mapping unless it is quoted — so a mapping here is refused with that said,
    rather than turned into text nobody's answer will ever contain.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"Project {project!r}: `label_findings:` must be a list of phrases.")
    phrases = []
    for phrase in value:
        if isinstance(phrase, dict):
            raise ValueError(
                f"Project {project!r}: a `label_findings:` phrase with a colon in it has "
                'to be quoted — `- "status: candidate"` — or YAML reads it as a mapping.'
            )
        if str(phrase).strip():
            phrases.append(str(phrase).strip())
    return tuple(phrases)


def _label_groups_from(project: str, value: Any) -> dict[str, tuple[str, ...]]:
    """`label_groups:` as a mapping of group name to its labels, in order.

    The order is kept because it is the order a group is searched in. A group
    written as a lone label is read as a group of one.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"Project {project!r}: `label_groups:` must be a mapping of name to labels."
        )
    groups: dict[str, tuple[str, ...]] = {}
    for name, labels in value.items():
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, list):
            raise ValueError(
                f"Project {project!r}: label group {str(name)!r} must be a list of labels."
            )
        groups[str(name)] = tuple(str(label).strip() for label in labels if str(label).strip())
    return groups


def _as_text(value: Any) -> str | None:
    """YAML types coerced to what the rest of the system expects.

    A chat id written unquoted is an int here and a string everywhere else, and
    the mismatch does not fail — it routes nowhere, quietly, which is the worst
    available outcome.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"expected a value, got the boolean {value!r}")
    return str(value).strip() or None


def _as_flag(project: str, key: str, value: Any) -> bool:
    """A yes-or-no setting. Absent is no; anything but a boolean is refused.

    Refused rather than read generously, because the generous reading of
    `label_work: "false"` is yes — a string is truthy — and this one writes to
    an issue tracker.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"`{key}:` in project {project!r} must be true or false, not {value!r}.")


def _task_label_from(label: str, project: str, value: Any) -> str | None:
    """A seat's own task label, refused if the tracker would split it in two.

    GitLab adds labels as a comma-separated list, so `task_label: a,b` would put
    two labels on every task the seat touched.
    """
    text = _as_text(value)
    if text and "," in text:
        raise ValueError(
            f"Seat {label!r} in project {project!r}: `task_label:` cannot contain a "
            "comma, which the tracker would read as two labels."
        )
    return text


def _seat_from(label: str, spec: Any, project: str) -> Seat:
    if not isinstance(spec, dict):
        raise ValueError(
            f"Seat {label!r} in project {project!r} must be a mapping of "
            f"{', '.join(sorted(_SEAT_FIELDS))}, not {type(spec).__name__}."
        )
    unknown = set(spec) - _SEAT_FIELDS
    if unknown:
        # Refused rather than ignored, exactly as the environment dialect does:
        # a seat missing the setting you believe you gave it, with nothing
        # anywhere saying so, is worse than a file that will not load.
        raise ValueError(
            f"Seat {label!r} in project {project!r}: unknown field(s) {', '.join(sorted(unknown))}"
        )

    runtime = (_as_text(spec.get("runtime")) or _default_runtime()).lower()
    allowed = known_runtimes()
    if runtime not in allowed:
        raise ValueError(
            f"Seat {label!r} has runtime {runtime!r}. Use one of: {', '.join(allowed)}."
        )
    role = _as_text(spec.get("role"))
    return Seat(
        label=label,
        runtime=runtime,
        session=_as_text(spec.get("session")),
        chat=_as_text(spec.get("chat")),
        role=Role(role.lower()) if role else None,
        project=project,
        after_compaction=_as_text(spec.get("after_compaction")),
        before_compaction=_as_text(spec.get("before_compaction")),
        task_label=_task_label_from(label, project, spec.get("task_label")),
    )


def projects_from_yaml(text: str) -> list[Project]:
    """Read the file into projects, each holding its own seats.

    Refuses anything it cannot honour. Every alternative loses a seat somebody
    believes exists: a duplicate label makes `find` ambiguous, an unknown field
    silently drops a setting, and an unreadable document that half-loads is
    worse than one that does not load at all.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None

    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        raise ValueError("The configuration must be a mapping with a `projects:` key.")

    raw_projects = loaded.get("projects")
    if raw_projects is None:
        return []
    if not isinstance(raw_projects, dict):
        raise ValueError("`projects:` must be a mapping of project name to its settings.")

    projects: list[Project] = []
    seen: dict[str, str] = {}
    for name, body in raw_projects.items():
        project = str(name)
        body = body or {}
        if not isinstance(body, dict):
            raise ValueError(f"Project {project!r} must be a mapping, not {type(body).__name__}.")
        unknown = set(body) - _PROJECT_FIELDS
        if unknown:
            raise ValueError(f"Project {project!r}: unknown field(s) {', '.join(sorted(unknown))}")

        raw_seats = body.get("seats") or {}
        if not isinstance(raw_seats, dict):
            raise ValueError(f"Project {project!r}: `seats:` must be a mapping of label to seat.")

        seats = []
        for label, spec in raw_seats.items():
            label = str(label)
            if label in seen:
                # Labels are how a seat is named in `doctor` and found by
                # `find`; two of them makes one unreachable and says nothing.
                raise ValueError(
                    f"Seat label {label!r} is used by both {seen[label]!r} and {project!r}. "
                    "Labels have to be unique across projects."
                )
            seen[label] = project
            seats.append(_seat_from(label, spec, project))

        # A sighting carries a runtime and a role and nothing else, so seats that
        # share both and ask for different task labels could not be told apart
        # when one of them is heard from. Refused here, where the fix is a line,
        # rather than labelling a task with whichever happened to match first.
        wanted: dict[tuple[str, Role | None], set[str | None]] = {}
        for seat in seats:
            wanted.setdefault((seat.runtime, seat.role), set()).add(seat.task_label)
        for (runtime, role), labels in wanted.items():
            if len(labels) > 1:
                shown = ", ".join(sorted(repr(x) if x else "the default" for x in labels))
                kind = f"{role.value} seats" if role else "seats without a role"
                raise ValueError(
                    f"Project {project!r}: its {runtime} {kind} ask for different "
                    f"task labels ({shown}), and nothing that labels a task can tell them apart."
                )

        path = _as_text(body.get("path"))
        older: list[str] = []
        written, spelled = _spelled(project, body, "inspections", "checks", "the project")
        if spelled is not None:
            older.append(spelled)
        inspections = _inspections_from(project, written)
        commands, command_lists = _commands_from(project, body.get("commands"))
        _not_a_list(project, body, command_lists)
        handoffs = _handoffs_from(
            project,
            body.get("handoffs"),
            inspections=inspections,
            seats=seats,
            commands=commands,
            older=older,
        )
        label_groups = _label_groups_from(project, body.get("label_groups"))
        validate = _validate_from(project, body.get("validate"), commands)
        _placeholders_checked(project, commands, label_groups, validate=validate, handoffs=handoffs)
        projects.append(
            Project(
                name=project,
                path=Path(path).expanduser() if path else None,
                seats=seats,
                validate=validate,
                warn_if=_warnings_from(project, body.get("warn_if")),
                commands=commands,
                command_lists=command_lists,
                forge=_as_text(body.get("forge")),
                labels=_warnings_from(project, body.get("labels")) or (),
                label_groups=label_groups,
                label_findings=_findings_from(project, body.get("label_findings")),
                label_work=_as_flag(project, "label_work", body.get("label_work")),
                confirmation=_confirmation_from(project, body.get("confirmation")),
                inspections=inspections,
                handoffs=handoffs,
                workflows=_workflows_from(
                    project, body.get("workflows"), handoffs=handoffs, seats=seats
                ),
                older=tuple(older),
            )
        )
    return projects


def from_yaml(text: str) -> list[Seat]:
    """Every seat in the file, flattened.

    The same shape `from_environment` returns, because everything downstream
    knows about seats and nothing downstream should have to learn about a file
    format to keep working.
    """
    return [seat for project in projects_from_yaml(text) for seat in project.seats]


def projects(directory: Path | None = None) -> list[Project]:
    """Every project described in the configuration, or none if there is none."""
    path = find_config(directory)
    if path is None:
        return []
    try:
        return projects_from_yaml(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None


def resolve_project(name: str, directory: Path | None = None) -> Path:
    """The directory a named project lives in.

    So `halyard wire alpha-engine` works: the configuration already says where
    every project is, and retyping the path is both tedious and a way to gate
    the wrong tree — a mistake that looks like success until a command runs
    somewhere nobody was watching.

    Raises rather than guessing. A project that is not described, and one
    described without a `path`, are different mistakes and get different
    sentences; neither is something to resolve on somebody's behalf.
    """
    described = projects(directory)
    if not described:
        raise ValueError(
            f"No halyard.yaml here, so {name!r} cannot be looked up. "
            "Give a directory instead, or run `halyard init`."
        )

    wanted = name.strip().casefold()
    for project in described:
        if project.name.casefold() != wanted:
            continue
        if project.path is None:
            raise ValueError(
                f"Project {project.name!r} has no `path:` in halyard.yaml, so there is "
                "nothing to wire. Add one, or give a directory instead."
            )
        if not project.path.is_dir():
            raise ValueError(
                f"Project {project.name!r} says its path is {project.path}, "
                "and that is not a directory on this machine."
            )
        return project.path

    known = ", ".join(project.name for project in described)
    raise ValueError(f"No project called {name!r}. The file describes: {known}")


def find_config(directory: Path | None = None) -> Path | None:
    """The YAML configuration, if there is one.

    Looked for beside `.env` rather than instead of it. A file that is not
    there is not an error: the environment dialect remains a complete way to
    configure this, and an installation that never writes YAML keeps working.
    """
    here = directory or Path.cwd()
    for name in ("halyard.yaml", "halyard.yml"):
        candidate = here / name
        if candidate.is_file():
            return candidate
    return None


def load(directory: Path | None = None) -> list[Seat]:
    """Seats from the YAML file if one exists, otherwise nothing.

    A file that exists and cannot be read raises. Falling back to the
    environment would be worse than failing: it would start the control plane
    with a configuration nobody wrote, holding seats somebody had replaced.
    """
    path = find_config(directory)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    try:
        return from_yaml(text)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None


def missing_files(projects: list[Project]) -> list[str]:
    """Every configured file that is not where the configuration says it is.

    Checked because nothing checked before. A Mac mini ran for weeks with none
    of its prompt files present: they were named in `halyard.yaml`, they were
    read at the moment they were needed, and a file that was not there produced
    a warning nobody was looking at and a compaction that quietly carried
    nothing. `doctor` did not look either, though a comment in the code claimed
    it did.

    Returns lines meant to be read by a person, each naming the project, the
    setting and the path. Empty when everything is where it should be — which is
    the answer worth being able to get in one look.

    Paths are relative to the project, because that is where these files live.
    """
    said: list[str] = []
    for project in projects:
        if project.path is None:
            continue

        def under(path: Path, root: Path = project.path) -> Path:
            return path if path.is_absolute() else root / path

        wanted: list[tuple[str, Path]] = []
        if project.confirmation:
            if project.confirmation.inquiry:
                wanted.append(("confirmation.inquiry", project.confirmation.inquiry))
            if project.confirmation.review:
                wanted.append(("confirmation.review", project.confirmation.review))
        for name, path in project.inspections.items():
            wanted.append((f"inspections.{name}", path))
        for name, handoff in project.handoffs.items():
            if handoff.prompt:
                wanted.append((f"handoffs.{name}.prompt", handoff.prompt))
            if handoff.followup_prompt:
                wanted.append((f"handoffs.{name}.followup_prompt", handoff.followup_prompt))
        for seat in project.seats:
            for key in ("before_compaction", "after_compaction"):
                if written := getattr(seat, key, None):
                    wanted.append((f"{seat.label}'s {key}", Path(written)))

        for setting, path in wanted:
            if not under(path).is_file():
                said.append(f"{project.name}: {setting} points at {path}, which is not there")
    return said


@dataclass(frozen=True)
class RuntimeSettings:
    """What one runtime is configured with, under `runtimes:` in the file.

    Everything here belongs to a runtime rather than to a project or a seat,
    and there was nowhere for that before. Model lists lived in the `settings:`
    block as `HALYARD_CLAUDE_MODELS=opus,sonnet,…`, which works for a
    comma-separated string and stops working the moment a runtime needs two
    settings of its own.
    """

    name: str
    #: Where this runtime's local server is, when it has one to talk to.
    #:
    #: opencode is the case, and the reason this exists. Its TUI serves an HTTP
    #: API — but only when started with `--port`, and its default is to pick
    #: nothing reachable. Measured: with `opencode` alone the process listens on
    #: no TCP port at all and the gate can deliver a question and never answer
    #: it; with `opencode --port 4096` both directions work.
    port: int | None = None
    #: Which models this runtime's seats may be switched between, in the order
    #: they should be offered. Empty means whatever the runtime itself says.
    models: tuple[str, ...] = ()
    #: Which of them to reach for when a provider says the quota is gone.
    #: Offered on a card rather than switched to: the other model costs
    #: different money, and that is not a decision to make on somebody's behalf.
    on_quota: str | None = None


def runtimes_from_yaml(text: str) -> dict[str, RuntimeSettings]:
    """The `runtimes:` block, by runtime name.

    Absent is the ordinary case and means nothing is configured — every runtime
    that predates this block works without one.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Could not read the configuration: {error}") from None
    if not isinstance(loaded, dict):
        return {}

    raw = loaded.get("runtimes")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("`runtimes:` must be a mapping of runtime name to its settings.")

    from halyard.agents import registry

    known = set(registry.names())
    found: dict[str, RuntimeSettings] = {}
    for name, body in raw.items():
        text_name = str(name).strip()
        if text_name not in known:
            # Refused rather than ignored. A misspelling here is a block of
            # settings that silently applies to nothing, and the way somebody
            # finds out is that the thing they configured does not happen.
            raise ValueError(
                f"`runtimes: {text_name}:` is not a runtime. Use one of: "
                f"{', '.join(sorted(known))}."
            )
        body = body or {}
        if not isinstance(body, dict):
            raise ValueError(f"`runtimes: {text_name}:` must be a mapping.")
        unknown = set(body) - {"port", "models", "on_quota"}
        if unknown:
            raise ValueError(
                f"`runtimes: {text_name}:` does not take {', '.join(sorted(unknown))}."
            )

        port = body.get("port")
        if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
            raise ValueError(f"`runtimes: {text_name}: port:` must be a port number.")

        models = body.get("models") or ()
        if isinstance(models, str) or not isinstance(models, list | tuple):
            raise ValueError(f"`runtimes: {text_name}: models:` must be a list.")

        on_quota = _as_text(body.get("on_quota"))
        if on_quota and models and on_quota not in [str(m) for m in models]:
            # Otherwise the card offers a model this configuration has not said
            # is usable here, and finding out costs a turn.
            raise ValueError(
                f"`runtimes: {text_name}: on_quota:` is {on_quota!r}, which is not in its "
                f"`models:` list."
            )

        found[text_name] = RuntimeSettings(
            name=text_name,
            port=port,
            models=tuple(str(m) for m in models),
            on_quota=on_quota,
        )
    return found


def runtime_settings(directory: Path | None = None) -> dict[str, RuntimeSettings]:
    """The `runtimes:` block from the configuration on disk."""
    path = find_config(directory)
    if path is None:
        return {}
    try:
        return runtimes_from_yaml(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Could not open {path}: {error}") from None
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
