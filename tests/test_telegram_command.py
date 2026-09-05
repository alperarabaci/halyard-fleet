"""Tests for `/command` — running a project's own commands from a phone.

Real commands in a real directory, because what could go wrong is the shell: a
command that fails, one that takes too long, one that has to see the project's
files. A double would agree with whatever this file already believes.

The one thing kept honest with a stub is *how long* — a test that waited an
hour to prove a timeout is not a test anybody runs.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from halyard.channels.telegram import cards
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.commands import catalogue, running
from halyard.core.approvals import ApprovalStore
from halyard.core.audit import AuditLog, JsonlAuditSink
from halyard.core.config_file import Project
from halyard.core.seats import Seat

CHAT = "-100777"
APPROVER = "4242"
INTRUDER = "9999"

COMMANDS = {
    "cleanup": "make cleanup-merged-branches",
    "bootstrap": "make bootstrap-up",
    "test-all": "make test-all",
}


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def set_my_commands(self, commands) -> None: ...

    async def send_message(self, chat_id, text, *, reply_markup=None, **kwargs) -> dict:
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent) + 100}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id, *, text=None): ...


@pytest.fixture
async def wired(tmp_path: Path):
    place = tmp_path / "alpha-engine"
    place.mkdir()
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeApi()
    project = Project(name="alpha-engine", path=place, seats=[], commands=dict(COMMANDS))
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        seats=[Seat(label="nav", runtime="claude-code", chat=CHAT, project="alpha-engine")],
        repositories={"alpha-engine": project},
        poll_retry_seconds=0.01,
    )
    try:
        yield channel, api, place
    finally:
        await audit.close()


def typed(text: str, *, user: str = APPROVER) -> dict:
    return {"message_id": 1, "from": {"id": int(user)}, "chat": {"id": CHAT}, "text": text}


def pressed(name: str, *, user: str = APPROVER) -> dict:
    return {
        "id": "cb1",
        "from": {"id": int(user)},
        "data": cards.choice_data("run", name),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


def teach(channel: TelegramChannel, **commands: str) -> None:
    """Replace what the project offers, as `halyard.yaml` would."""
    from dataclasses import replace

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands=commands)


async def finished(channel: TelegramChannel) -> None:
    """Wait for whatever `/command` started, and for it to have reported.

    The run is detached on purpose — that is the feature — so a test has to
    wait for it the way a person does.
    """
    for _ in range(400):
        await asyncio.sleep(0.02)
        if not channel._working:
            await asyncio.sleep(0.05)
            return


async def settled(channel: TelegramChannel) -> None:
    """Wait for whatever the command detached.

    `/commit`, `/label` and the commit buttons run off the poll loop on purpose
    — a project's test suite must not stop everybody else's approvals being
    answered — so a test has to wait for them the way a person does.
    """
    for _ in range(400):
        pending = [task for task in channel._sending if not task.done()]
        if not pending:
            await asyncio.sleep(0.02)
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def deliver(channel: TelegramChannel, update: dict) -> None:
    await channel._handle_message(update)
    await settled(channel)


async def tap(channel: TelegramChannel, callback: dict) -> None:
    await channel._handle_callback(callback)
    await settled(channel)


# --- what is offered --------------------------------------------------------


async def test_a_bare_command_asks_which_one(wired) -> None:
    """Telegram's menu pastes `/command` and stops, so the useful next thing is
    the question rather than a list to type from."""
    channel, api, _ = wired

    await deliver(channel, typed("/command"))

    assert "Run which one?" in api.sent[-1]["text"]
    offered = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert offered == {"cleanup", "bootstrap", "test-all"}


async def test_a_project_with_no_commands_says_so(wired) -> None:
    channel, api, _ = wired
    teach(channel)

    await deliver(channel, typed("/command"))

    assert "lists no commands" in api.sent[-1]["text"]


async def test_a_name_nobody_knows_says_so_and_then_asks(wired) -> None:
    channel, api, _ = wired

    await deliver(channel, typed("/command deploy-to-production"))

    assert "no command called" in api.sent[-2]["text"]
    assert "Run which one?" in api.sent[-1]["text"]


# --- running ----------------------------------------------------------------


async def test_a_command_that_passes_reports_its_tail(wired) -> None:
    channel, api, _ = wired
    teach(channel, greet="echo one; echo two; echo three")

    await deliver(channel, typed("/command greet"))
    await finished(channel)

    assert "Running" in api.sent[0]["text"]
    assert "finished in" in api.sent[-1]["text"]
    assert "three" in api.sent[-1]["text"]


async def test_a_command_that_fails_says_so_and_shows_more(wired) -> None:
    """A test runner puts what broke at the end, and one line of it is a riddle."""
    channel, api, _ = wired
    teach(channel, broken="echo 'FAILED tests/test_thing.py::test_one'; exit 2")

    await deliver(channel, typed("/command broken"))
    await finished(channel)

    assert "failed after" in api.sent[-1]["text"]
    assert "test_thing.py::test_one" in api.sent[-1]["text"]


async def test_it_runs_inside_the_project(wired) -> None:
    channel, api, place = wired
    (place / "Makefile").write_text("all:\n\techo hi\n")
    teach(channel, here="test -f Makefile && echo found-it")

    await deliver(channel, typed("/command here"))
    await finished(channel)

    assert "found-it" in api.sent[-1]["text"]


async def test_pressing_a_button_runs_that_command(wired) -> None:
    channel, api, _ = wired
    teach(channel, greet="echo pressed")

    await tap(channel, pressed("greet"))
    await finished(channel)

    assert "pressed" in api.sent[-1]["text"]


async def test_somebody_else_pressing_it_runs_nothing(wired) -> None:
    channel, _, place = wired
    teach(channel, touch=f"touch {place / 'ran'}")

    await tap(channel, pressed("touch", user=INTRUDER))
    await finished(channel)

    assert not (place / "ran").exists()


# --- one at a time ----------------------------------------------------------


async def test_a_second_command_is_refused_while_one_runs(wired) -> None:
    """Two `make` runs in one directory fight over the same build outputs, and
    the second one's failure is a mystery."""
    channel, api, _ = wired
    teach(channel, slow="sleep 0.4", quick="echo quick")

    # Deliberately not waiting for the first: the second arrives *while* it
    # runs, which is the whole case.
    await channel._handle_message(typed("/command slow"))
    await channel._handle_message(typed("/command quick"))

    assert "still running" in api.sent[-1]["text"]
    await finished(channel)


async def test_the_project_is_free_again_once_it_finishes(wired) -> None:
    channel, api, _ = wired
    teach(channel, quick="echo done")

    await deliver(channel, typed("/command quick"))
    await finished(channel)
    assert channel._working == {}

    await deliver(channel, typed("/command quick"))
    await finished(channel)
    assert "finished in" in api.sent[-1]["text"]


async def test_a_command_that_could_not_start_frees_the_project(wired, monkeypatch) -> None:
    """A project left marked busy by a crash would refuse everything afterwards
    for no reason anybody could see."""
    channel, api, _ = wired
    teach(channel, boom="echo hi")

    def explode(*args, **kwargs):
        raise RuntimeError("no shell today")

    monkeypatch.setattr(running, "run", explode)
    await deliver(channel, typed("/command boom"))
    await finished(channel)

    assert channel._working == {}
    assert "could not be started" in api.sent[-1]["text"]


# --- the runner itself ------------------------------------------------------


def test_a_run_that_outlives_its_clock_is_reported_as_stopped(tmp_path: Path) -> None:
    result = running.run("sleep 5", tmp_path, timeout=0.2)

    assert result.timed_out is True
    assert result.ok is False
    assert "still running" in result.output


def test_a_passing_run_keeps_only_its_last_lines(tmp_path: Path) -> None:
    result = running.run("seq 1 100", tmp_path)

    assert result.ok is True
    assert result.output.splitlines() == [str(n) for n in range(96, 101)]


def test_a_failing_run_keeps_more_of_them(tmp_path: Path) -> None:
    result = running.run("seq 1 100 && exit 1", tmp_path)

    assert result.ok is False
    assert len(result.output.splitlines()) == running.LINES_WHEN_IT_FAILED


def test_nothing_it_runs_can_ask_a_person_anything(tmp_path: Path) -> None:
    """A `make` target that shells out to git for a credential would otherwise
    block until the timeout for an answer nobody is there to give."""
    result = running.run("echo $GIT_TERMINAL_PROMPT:$CI", tmp_path)

    assert result.output.strip() == "0:1"


def test_a_command_that_cannot_be_started_is_answered_not_raised(tmp_path: Path) -> None:
    result = running.run("echo hi", tmp_path / "not-a-directory")

    assert result.ok is False
    assert result.output


# --- the catalogue ----------------------------------------------------------


def test_commands_keep_the_order_they_were_written() -> None:
    listed = catalogue.offered({"b": "echo b", "a": "echo a"})

    assert [c.name for c in listed] == ["b", "a"]


def test_an_entry_missing_its_command_is_skipped_not_fatal() -> None:
    listed = catalogue.offered({"empty": "", "fine": "echo fine"})

    assert [c.name for c in listed] == ["fine"]


def test_a_name_too_long_for_a_button_is_refused() -> None:
    listed = catalogue.offered({"x" * 60: "echo x", "fine": "echo fine"})

    assert [c.name for c in listed] == ["fine"]


def test_a_name_is_matched_however_it_was_typed() -> None:
    assert catalogue.resolve(COMMANDS, "  TEST-ALL ").line == "make test-all"
    assert catalogue.resolve(COMMANDS, "nothing") is None


def test_terminal_colour_is_stripped_from_what_the_phone_sees(tmp_path: Path) -> None:
    """Measured on a real Makefile whose `help` target writes the escapes
    itself, which `CI=1` does nothing about. A phone renders them as litter."""
    result = running.run(r"printf '\033[36mgreen\033[0m line\n'", tmp_path)

    assert result.output == "green line"
    assert "\x1b" not in result.output


# --- /label ------------------------------------------------------------------


class FakeForge:
    """A forge that answers from memory, recording what it was asked to add."""

    name = "GitLab"

    def __init__(self, *, on_task=("backend",), defined=("andon", "rework", "backend")) -> None:
        self.on_task = tuple(on_task)
        self.defined = tuple(defined)
        self.added: list[tuple[int, str]] = []
        self.refuse: Exception | None = None

    async def task(self, number: int):
        from halyard.tasks.spec import Task

        if self.refuse:
            raise self.refuse
        return Task(number=number, title="RAG v4 PDF report", labels=self.on_task)

    async def labels(self) -> tuple[str, ...]:
        if self.refuse:
            raise self.refuse
        return self.defined

    async def add_label(self, number: int, label: str):
        from halyard.tasks.spec import Task

        if self.refuse:
            raise self.refuse
        self.added.append((number, label))
        return Task(number=number, title="RAG v4 PDF report", labels=(*self.on_task, label))


def on_a_task(channel: TelegramChannel, monkeypatch, forge: FakeForge, *, branch="320-thing"):
    """Put the project on a task-named branch with a forge behind it."""
    from halyard import tasks as tracker
    from halyard.channels.telegram import adapter as under_test

    monkeypatch.setattr(under_test.task_tracker, "current_branch", lambda path: branch)
    monkeypatch.setattr(
        under_test.task_tracker,
        "origin_of",
        lambda path: tracker.Origin(host="gitlab.com", path="a/b"),
    )
    monkeypatch.setattr(under_test.task_tracker, "build", lambda *a, **k: forge)
    channel._forge_token = "glpat-x"
    return forge


def label_pressed(name: str, *, user: str = APPROVER) -> dict:
    return {
        "id": "cb1",
        "from": {"id": int(user)},
        "data": cards.choice_data("label", name),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_label_names_the_task_and_offers_what_is_not_on_it(wired, monkeypatch) -> None:
    """A button for a label the task already has is a wasted tap."""
    channel, api, _ = wired
    on_a_task(channel, monkeypatch, FakeForge())

    await deliver(channel, typed("/label"))

    said = api.sent[-1]["text"]
    assert "#320" in said and "RAG v4 PDF report" in said
    assert "backend" in said  # said as already on it
    offered = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert offered == {"andon", "rework"}


async def test_a_project_can_narrow_which_labels_are_offered(wired, monkeypatch) -> None:
    from dataclasses import replace as _replace

    channel, api, _ = wired
    on_a_task(channel, monkeypatch, FakeForge(defined=("andon", "rework", "wontfix", "duplicate")))
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = _replace(found, labels=("andon", "rework"))

    await deliver(channel, typed("/label"))

    offered = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert offered == {"andon", "rework"}


async def test_pressing_a_label_adds_exactly_that_one(wired, monkeypatch) -> None:
    channel, api, _ = wired
    forge = on_a_task(channel, monkeypatch, FakeForge())

    await tap(channel, label_pressed("andon"))

    assert forge.added == [(320, "andon")]
    assert "andon" in api.sent[-1]["text"]
    assert "#320" in api.sent[-1]["text"]


async def test_somebody_else_pressing_a_label_adds_nothing(wired, monkeypatch) -> None:
    channel, _, _ = wired
    forge = on_a_task(channel, monkeypatch, FakeForge())

    await tap(channel, label_pressed("andon", user=INTRUDER))

    assert forge.added == []


async def test_a_branch_not_named_for_a_task_says_so(wired, monkeypatch) -> None:
    channel, api, _ = wired
    on_a_task(channel, monkeypatch, FakeForge(), branch="feat/runtime-isolation")

    await deliver(channel, typed("/label"))

    assert "not named for a task" in api.sent[-1]["text"]


async def test_a_task_with_every_label_already_on_it_says_so(wired, monkeypatch) -> None:
    channel, api, _ = wired
    on_a_task(channel, monkeypatch, FakeForge(on_task=("andon", "rework", "backend")))

    await deliver(channel, typed("/label"))

    assert "Nothing left to add" in api.sent[-1]["text"]
    assert api.sent[-1].get("reply_markup") is None


async def test_what_the_forge_refused_is_what_the_phone_is_told(wired, monkeypatch) -> None:
    from halyard.tasks.spec import ForgeError

    channel, api, _ = wired
    forge = FakeForge()
    forge.refuse = ForgeError("GitLab refused the token.")
    on_a_task(channel, monkeypatch, forge)

    await deliver(channel, typed("/label"))

    assert "refused the token" in api.sent[-1]["text"]


async def test_no_token_configured_says_which_problem_that_is(wired, monkeypatch) -> None:
    from halyard import tasks as tracker
    from halyard.channels.telegram import adapter as under_test

    channel, api, _ = wired
    monkeypatch.setattr(under_test.task_tracker, "current_branch", lambda path: "320-thing")
    monkeypatch.setattr(
        under_test.task_tracker,
        "origin_of",
        lambda path: tracker.Origin(host="gitlab.com", path="a/b"),
    )
    channel._forge_token = None

    await deliver(channel, typed("/label"))

    assert "No token" in api.sent[-1]["text"]


# --- /doctor -----------------------------------------------------------------


async def test_doctor_brings_the_same_check_to_the_phone(wired, monkeypatch) -> None:
    """Somebody was told to "check doctor" while away from the machine and
    could not. A check nobody can reach is a check nobody runs."""
    channel, api, _ = wired

    def clean() -> int:
        print("ok    everything is fine")
        print("ok    and the columns line up")
        return 0

    monkeypatch.setattr("halyard.doctor.run", clean)
    await deliver(channel, typed("/doctor"))

    assert "Nothing wrong here" in api.sent[-2]["text"]
    assert "the columns line up" in api.sent[-1]["text"]


async def test_doctor_says_how_many_problems_there_are(wired, monkeypatch) -> None:
    channel, api, _ = wired

    def unhappy() -> int:
        print("FAIL  two things")
        return 2

    monkeypatch.setattr("halyard.doctor.run", unhappy)
    await deliver(channel, typed("/doctor"))

    assert "2 problems" in api.sent[-2]["text"]


async def test_a_check_that_itself_fails_says_so(wired, monkeypatch) -> None:
    """Rather than an empty message, which reads as everything being fine."""
    channel, api, _ = wired

    def explode() -> int:
        raise RuntimeError("no")

    monkeypatch.setattr("halyard.doctor.run", explode)
    await deliver(channel, typed("/doctor"))

    assert "check itself failed" in api.sent[-1]["text"]


async def test_the_output_is_escaped_so_telegram_does_not_refuse_it(wired, monkeypatch) -> None:
    """`doctor` prints paths and command lines, and one `<` in the wrong place
    makes Telegram reject the whole message."""
    channel, api, _ = wired

    def angular() -> int:
        print("ok    reads <halyard.yaml> & friends")
        return 0

    monkeypatch.setattr("halyard.doctor.run", angular)
    await deliver(channel, typed("/doctor"))

    assert "&lt;halyard.yaml&gt;" in api.sent[-1]["text"]
    assert "&amp;" in api.sent[-1]["text"]
