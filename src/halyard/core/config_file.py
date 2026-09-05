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
    "confirmation",
}
_SEAT_FIELDS = {"runtime", "session", "chat", "role", "after_compaction", "before_compaction"}


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
class Project:
    """A codebase, its location, and the seats working in it."""

    name: str
    #: Where to wire the gate. Optional: a project can be described before
    #: anybody decides to gate it.
    path: Path | None
    seats: list[Seat]
    #: What has to pass before a commit is offered from a phone — `make
    #: test-fast`, or whatever this project calls its quick check. Optional,
    #: and absent means no check runs rather than some guessed default: a
    #: command invented for somebody's repository would fail on every commit.
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
    #: Which kind of issue tracker this project's remote points at. Only needed
    #: for a host that does not name itself — `gitlab.com` does, and
    #: `git.example.com` cannot.
    forge: str | None = None
    #: Which labels `/label` offers. Empty means every label the project has,
    #: which is the right default until a project has more of them than a phone
    #: keyboard can show.
    labels: tuple[str, ...] = ()
    #: The extra round this project asks for before closing a piece of work.
    #: `None` means no such round exists here, and `/commit` is unchanged.
    confirmation: Confirmation | None = None


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


def _commands_from(project: str, value: Any) -> dict[str, str]:
    """`commands:` as a mapping of name to command line."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Project {project!r}: `commands:` must be a mapping of name to command.")
    return {str(name): str(line) for name, line in value.items()}


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

        path = _as_text(body.get("path"))
        projects.append(
            Project(
                name=project,
                path=Path(path).expanduser() if path else None,
                seats=seats,
                validate=_as_text(body.get("validate")),
                warn_if=_warnings_from(project, body.get("warn_if")),
                commands=_commands_from(project, body.get("commands")),
                forge=_as_text(body.get("forge")),
                labels=_warnings_from(project, body.get("labels")) or (),
                confirmation=_confirmation_from(project, body.get("confirmation")),
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
        for seat in project.seats:
            for key in ("before_compaction", "after_compaction"):
                if written := getattr(seat, key, None):
                    wanted.append((f"{seat.label}'s {key}", Path(written)))

        for setting, path in wanted:
            if not under(path).is_file():
                said.append(f"{project.name}: {setting} points at {path}, which is not there")
    return said
