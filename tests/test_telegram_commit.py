"""Tests for `/commit` — the Telegram half.

`tests/test_commit_repository.py` covers what git is asked and how its answers are read.
This covers the part that can commit something nobody agreed to: which button
does it, who is allowed to press it, and what happens when it is pressed twice.

The repository is real, so a passing test here means a commit actually landed.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from halyard.agents.base import SessionRef
from halyard.channels.telegram import commit_card
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.core.approvals import ApprovalStore
from halyard.core.audit import AuditLog, JsonlAuditSink
from halyard.core.config_file import Project
from halyard.core.events import Role
from halyard.core.registry import SessionRegistry
from halyard.core.seats import Seat

CHAT = "-100777"
APPROVER = "4242"
INTRUDER = "9999"


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []
        self._next = 100

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def set_my_commands(self, commands) -> None: ...

    async def send_message(self, chat_id, text, *, reply_markup=None, **kwargs) -> dict:
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next}

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None, **kwargs):
        self.edits.append({"message_id": message_id, "text": text})
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id, *, text=None):
        self.answers.append({"text": text})


class FakeRunner:
    """Writes a fixed subject line, and remembers what it was asked."""

    id = "claude-code"
    available = True

    def __init__(self, *, says: str | None = "loader stub and seed tweak") -> None:
        self.says = says
        self.asked: list[str] = []
        self.models: list[str | None] = []
        self.sent: list[tuple[str, str]] = []
        #: Session names this runtime claims to know, as the real one would.
        self.sessions: dict[str, object] = {}
        #: Whether a session takes what is sent to it, as `send` answers.
        self.accepting = True

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        self.asked.append(text)
        self.models.append(model)
        if self.says is None:
            raise RuntimeError("no model today")
        return self.says

    def resolve(self, name: str):
        """What the channel asks its seat's runtime a session name means."""
        return self.sessions.get(name)

    def busy(self, session_id: str) -> bool:
        return False

    def preferences(self, session_id: str) -> tuple[str | None, str | None]:
        return (None, None)

    async def send(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        when_done=None,
    ) -> bool:
        """What reaches a session, as against what reaches the chat."""
        self.sent.append((session_id, text))
        return self.accepting


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    place = tmp_path / "alpha-engine"
    place.mkdir()
    git(place, "init", "-q", "-b", "281-power-gen-minor-fixes")
    git(place, "config", "user.email", "t@example.com")
    git(place, "config", "user.name", "Tester")
    (place / "seed.txt").write_text("a\n")
    git(place, "add", ".")
    git(place, "commit", "-qm", "alpha-engine#279 p2")
    return place


@pytest.fixture
async def wired(tmp_path: Path, repo: Path):
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeApi()
    runner = FakeRunner()
    # The seat's session, as the runtime would report it. Without this the
    # channel has a seat it cannot reach and says so instead of delivering.
    runner.sessions["alpha-engine-navigator"] = SessionRef(
        "id-nav", "alpha-engine-navigator", str(repo), None, None
    )
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        seats=[
            Seat(
                label="nav",
                runtime="claude-code",
                chat=CHAT,
                project="alpha-engine",
                role=Role.NAVIGATOR,
                session="alpha-engine-navigator",
            )
        ],
        runners={"claude-code": runner},
        registry=SessionRegistry(),
        repositories={"alpha-engine": Project(name="alpha-engine", path=repo, seats=[])},
        poll_retry_seconds=0.01,
    )
    try:
        yield channel, api, runner, repo
    finally:
        await audit.close()


def typed(text: str, *, user: str = APPROVER) -> dict:
    return {"message_id": 1, "from": {"id": int(user)}, "chat": {"id": CHAT}, "text": text}


def replying(text: str, to: str, *, user: str = APPROVER) -> dict:
    return {
        "message_id": 9,
        "from": {"id": int(user)},
        "chat": {"id": CHAT},
        "text": text,
        "reply_to_message": {"message_id": 7, "text": to},
    }


def press(handle: str, action: str, *, user: str = APPROVER, message_id: int = 101) -> dict:
    return {
        "id": "cb1",
        "from": {"id": int(user)},
        "data": commit_card.callback_data(handle, action),
        "message": {"message_id": message_id, "chat": {"id": CHAT}},
    }


def wrote(repo: Path, name: str, text: str) -> None:
    """An agent writing a file. Nothing stages it, which is the point."""
    (repo / name).write_text(text)


def only_handle(channel: TelegramChannel) -> str:
    handles = list(channel._proposals._open)
    assert len(handles) == 1, handles
    return handles[0]


def subject(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%s").strip()


def commit_count(repo: Path) -> int:
    return len(git(repo, "log", "--format=%h").splitlines())


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


# --- proposing --------------------------------------------------------------


async def test_a_card_offers_the_message_and_what_it_would_commit(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "def load():\n    return 1\n")

    await deliver(channel, typed("/commit"))

    card = api.sent[-1]
    assert "alpha-engine#281 loader stub and seed tweak" in card["text"]
    assert "281-power-gen-minor-fixes" in card["text"]
    assert "loader.py" in card["text"]
    assert card["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Commit"
    # Proposed, not committed. Nothing has happened to the repository yet.
    assert commit_count(repo) == 1


async def test_the_model_is_asked_with_the_house_style_and_the_cheap_model(wired) -> None:
    channel, _, runner, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert runner.models == ["sonnet"]
    assert "alpha-engine#279 p2" in runner.asked[0]


async def test_what_an_agent_wrote_without_staging_is_offered(wired) -> None:
    """The case the first version got wrong: nobody staged anything, and there
    is still an afternoon of work to commit."""
    channel, api, _, repo = wired
    (repo / "written_by_an_agent.py").write_text("def load():\n    return 1\n")

    await deliver(channel, typed("/commit"))

    assert "written_by_an_agent.py" in api.sent[-1]["text"]
    assert "1 of them new" in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


async def test_a_clean_branch_says_there_is_nothing_to_commit(wired) -> None:
    channel, api, _, repo = wired

    await deliver(channel, typed("/commit"))

    assert "Nothing has changed" in api.sent[-1]["text"]
    assert len(channel._proposals) == 0
    assert commit_count(repo) == 1


async def test_a_chat_with_no_repository_says_so(tmp_path: Path) -> None:
    audit = AuditLog([JsonlAuditSink(tmp_path / "audit.jsonl")])
    await audit.open()
    api = FakeApi()
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        poll_retry_seconds=0.01,
    )
    try:
        await deliver(channel, typed("/commit"))
        said = api.sent[-1]["text"]
        assert "do not know which repository" in said
        assert f"no seat has it. Give a seat <code>chat: {CHAT}</code>" in said
    finally:
        await audit.close()


async def test_a_chat_whose_project_has_no_path_says_whose_chat_it_is(wired) -> None:
    """The seat is the part nobody can see from the chat, and it can be one
    nobody remembers setting up."""
    channel, api, _, _ = wired
    # A project written without a `path:` never reaches the channel.
    channel._repositories.clear()

    await deliver(channel, typed("/handoff"))

    assert (
        "it is <b>nav</b>'s, and nav's project <b>alpha-engine</b> has no <code>path:</code>"
        in api.sent[-1]["text"]
    )


async def test_a_chat_whose_seat_is_in_no_project_says_so(wired) -> None:
    channel, api, _, _ = wired
    channel._seats = [replace(seat, project=None) for seat in channel._seats]
    # With a single project every chat is about it, so there is none here.
    channel._repositories.clear()

    await deliver(channel, typed("/checks"))

    assert "it is <b>nav</b>'s, and nav is under no project" in api.sent[-1]["text"]


async def test_a_model_that_cannot_be_reached_still_offers_the_reference(wired) -> None:
    """Failing soft. The branch already names the issue, and Rewrite is one tap
    away — losing the commit because a model was busy would be the worse end."""
    channel, api, runner, repo = wired
    runner.says = None
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert "alpha-engine#281" in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


# --- committing -------------------------------------------------------------


async def test_pressing_commit_makes_the_commit(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"
    assert commit_count(repo) == 2
    assert "COMMITTED" in api.edits[-1]["text"]


async def test_pressing_commit_twice_makes_one_commit(wired) -> None:
    """There is no nonce here — the proposal is dropped as it is used, and that
    is what a second tap runs into."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)

    await tap(channel, press(handle, commit_card.MAKE))
    await tap(channel, press(handle, commit_card.MAKE))

    assert commit_count(repo) == 2
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_cancel_leaves_the_repository_alone(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.DROP))

    assert commit_count(repo) == 1
    assert "CANCELLED" in api.edits[-1]["text"]
    assert len(channel._proposals) == 0


async def test_somebody_else_pressing_commit_commits_nothing(wired) -> None:
    """The card is visible to a whole group. Checked exactly as an approval is."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.MAKE, user=INTRUDER))

    assert commit_count(repo) == 1
    assert len(channel._proposals) == 1
    assert api.edits == []


async def test_a_proposal_left_too_long_is_not_committable(wired) -> None:
    """The card describes a staging area as it was. A button tapped tomorrow
    would commit whatever is staged then, under a message written for this."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)

    channel._proposals._open[handle] = replace(
        channel._proposals._open[handle], at=channel._clock() - timedelta(hours=2)
    )
    await tap(channel, press(handle, commit_card.MAKE))

    assert commit_count(repo) == 1
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_git_refusing_is_reported_rather_than_swallowed(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    # Taken away behind the card's back, so there is nothing left to commit.
    (repo / "loader.py").unlink()

    await tap(channel, press(handle, commit_card.MAKE))

    assert commit_count(repo) == 1
    assert "git refused" in api.sent[-1]["text"]


# --- rewriting --------------------------------------------------------------


async def test_rewrite_asks_for_a_message_and_commits_nothing(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)

    await tap(channel, press(handle, commit_card.REWRITE))

    assert api.sent[-1]["reply_markup"] == {"force_reply": True}
    assert handle in api.sent[-1]["text"]
    assert commit_count(repo) == 1
    # Kept, not taken: the sentence still has to find it.
    assert handle in channel._proposals


async def test_a_typed_message_replaces_the_wording_without_committing(wired) -> None:
    """One thing in this flow commits, and it is the Commit button. A typo
    typed on a phone must not be a commit nobody agreed to."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]

    await deliver(channel, replying("power gen minor fixes", asked))

    assert commit_count(repo) == 1
    assert channel._proposals.peek(handle).message == "alpha-engine#281 power gen minor fixes"
    assert "alpha-engine#281 power gen minor fixes" in api.sent[-1]["text"]

    await tap(channel, press(handle, commit_card.MAKE))
    assert subject(repo) == "alpha-engine#281 power gen minor fixes"


async def test_a_reply_to_a_commit_prompt_never_reaches_a_session(wired) -> None:
    """The hand-off that routes sentences to seats would otherwise claim this
    one — which is how `/to` sent two messages to an agent nobody chose."""
    channel, api, runner, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]
    before = len(runner.asked)

    await deliver(channel, replying("power gen minor fixes", asked))

    assert len(runner.asked) == before


async def test_a_rewrite_of_a_proposal_that_expired_says_so(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]
    channel._proposals._open.clear()

    await deliver(channel, replying("anything", asked))

    assert "no longer open" in api.sent[-1]["text"]
    assert commit_count(repo) == 1


# --- the command itself -----------------------------------------------------


def test_commit_is_registered_so_it_appears_when_you_type_a_slash() -> None:
    from halyard.channels.telegram.adapter import COMMANDS

    assert ("commit", "Commit this branch's work, with a message to approve") in COMMANDS


# --- saying it happened, and pushing ----------------------------------------


# --- /checks: a project's own checks over the last reply ----------------------
#
# Here because it runs on the same two things `/commit` does: the project's
# repository, and a one-shot model turn.


def checks_in(channel: TelegramChannel, repo: Path, tmp_path: Path, **texts: str) -> None:
    """Give the project checks on disk, and a reply in the chat to check."""
    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said

    (repo / "NOTES").mkdir(exist_ok=True)
    for name, text in texts.items():
        (repo / "NOTES" / f"{name}.md").write_text(text)
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, checks={name: Path(f"NOTES/{name}.md") for name in texts}
    )
    channel._said_path = tmp_path / "last-said.json"
    last_said.remember(channel._kept(CHAT, SAID_FILE), chat_id=CHAT, text="All 42 tests passed.")


def pressed_check(name: str) -> dict:
    """The button a check is offered on, pressed by somebody allowed to."""
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data("check", name),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_a_bare_checks_offers_each_check_as_a_button(tmp_path: Path, wired) -> None:
    """The shape `/label` and `/command` already have: pick one, and it runs —
    or cancel, and nothing does."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof", claims="# claims")

    await channel._run_checks("", CHAT, None)

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["proof", "claims"]
    assert rows[-1] == [cards.CANCEL]
    assert runner.asked == []


async def test_cancelling_a_choice_card_takes_its_buttons_away(tmp_path: Path, wired) -> None:
    """The card stays, so the chat still shows what was offered; the buttons go,
    so nothing on it can be pressed by mistake later. Nothing runs."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_callback(
        {
            "id": "cb1",
            "from": {"id": int(APPROVER)},
            "data": cards.CANCEL["callback_data"],
            "message": {"message_id": 5, "chat": {"id": CHAT}, "text": "Check with which one?"},
        }
    )
    await settled(channel)

    assert api.edits[-1]["text"].startswith("Check with which one?")
    assert api.edits[-1]["text"].endswith("✖️ Cancelled")
    assert runner.asked == []


async def test_a_check_is_logged_as_what_it_was_given_and_what_it_said(
    tmp_path: Path, wired, caplog
) -> None:
    """A frame rather than the files: enough to say afterwards what a finding
    was about, and which revision of the check found it."""
    import logging

    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    caplog.set_level(logging.INFO)

    await channel._run_checks("proof delivery", CHAT, None)

    asked = next(r.getMessage() for r in caplog.records if "Check proof asked" in r.getMessage())
    assert "alpha-engine#281" in asked
    assert "NOTES/proof.md @ uncommitted" in asked
    assert "'delivery'" in asked
    assert "Check proof answered" in caplog.text
    assert runner.says in caplog.text


async def test_the_reply_is_timed_by_this_machines_clock(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Kept in UTC, shown in local time: a reply that arrived at 23:20 in
    Istanbul was shown as 20:20, and read as three hours old."""
    import time
    from datetime import UTC, datetime

    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said

    channel, api, _, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    last_said.remember(
        channel._kept(CHAT, SAID_FILE),
        chat_id=CHAT,
        text="done",
        now=datetime(2026, 9, 13, 20, 20, tzinfo=UTC),
    )
    monkeypatch.setenv("TZ", "Europe/Istanbul")
    time.tzset()
    try:
        await channel._run_checks("", CHAT, None)
    finally:
        monkeypatch.undo()
        time.tzset()

    assert "23:20" in api.sent[-1]["text"]


def pressed_result(value: str) -> dict:
    """A seat's button under a check's answer."""
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data("result", value),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_the_answer_carries_a_button_per_seat(tmp_path: Path, wired) -> None:
    """On the answer itself, so handing it on is one tap from reading it."""
    from halyard.channels.telegram import cards

    channel, api, _, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_checks("proof", CHAT, None)

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "→ nav"
    assert rows[-1] == [cards.CANCEL]


async def test_a_seats_button_hands_it_the_whole_answer_and_what_it_is(
    tmp_path: Path, wired
) -> None:
    """With the line that says what it is, so the session can tell a finding
    from an instruction."""
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    await channel._run_checks("proof", CHAT, None)

    await channel._handle_callback(pressed_result("proof>nav"))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-nav"
    assert "To nav (navigator), from Halyard." in text
    assert "Check: proof — NOTES/proof.md @ uncommitted" in text
    assert "alpha-engine#281" in text
    # The findings, then the reply they are about: a finding about a report the
    # reader does not have is a finding nobody can weigh.
    assert text.index(runner.says) < text.index("All 42 tests passed.")


async def test_the_button_under_one_check_sends_that_checks_answer(tmp_path: Path, wired) -> None:
    """A chat holds several checks' answers; the one under `proof` sends proof's."""
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof", claims="# claims")
    runner.says = "proof found this"
    await channel._run_checks("proof", CHAT, None)
    runner.says = "claims found that"
    await channel._run_checks("claims", CHAT, None)

    await channel._handle_callback(pressed_result("proof>nav"))
    await settled(channel)

    [(_, text)] = runner.sent
    assert "proof found this" in text
    assert "claims found that" not in text


async def test_an_answer_no_longer_kept_says_so(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_callback(pressed_result("proof>nav"))
    await settled(channel)

    assert "no longer kept" in api.sent[-1]["text"]
    assert runner.sent == []


async def test_what_a_chat_heard_is_kept_under_its_project(tmp_path: Path, wired) -> None:
    """As `halyard.yaml` nests them: a project's agent prose stays with that
    project, its bound is its own, and removing it leaves nothing mixed in."""
    channel, *_ = wired
    channel._said_path = tmp_path / "last-said.json"

    await channel.send_message(
        "id-nav", "the plan", None, agent_id="claude-code", session_name="alpha-engine-navigator"
    )

    assert (tmp_path / "projects" / "alpha-engine" / "last-said.json").is_file()
    assert not (tmp_path / "last-said.json").exists()


async def test_a_chat_no_project_owns_keeps_the_machine_file(tmp_path: Path, wired) -> None:
    channel, *_ = wired
    channel._said_path = tmp_path / "last-said.json"
    # Two projects, so a chat no seat owns belongs to neither.
    channel._repositories["beta"] = Project(name="beta", path=tmp_path, seats=[])

    assert channel._kept("-100999", "last-said.json") == tmp_path / "last-said.json"


async def test_a_project_name_cannot_climb_out_of_where_state_is_kept(
    tmp_path: Path, wired
) -> None:
    """A directory named by configuration must not be able to leave `projects/`."""
    channel, *_ = wired
    channel._said_path = tmp_path / "last-said.json"
    channel._seats = [
        Seat(label="nav", runtime="claude-code", chat=CHAT, project="../evil", session="x")
    ]

    kept = channel._kept(CHAT, "last-said.json")

    assert kept.parent.parent == tmp_path / "projects"


# --- /handoff: a reply handed on the way the project defines it ---------------


def handoffs_in(channel, repo: Path, tmp_path: Path, runner, **specs: dict) -> None:
    """A reviewer seat, a check, the handoffs asked for, and a reply to hand on."""
    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said
    from halyard.core.config_file import Handoff

    (repo / "NOTES").mkdir(exist_ok=True)
    (repo / "NOTES" / "review.md").write_text("You did not write this prompt; try to break it.")
    (repo / "NOTES" / "proof.md").write_text("# proof")
    channel._seats = [
        *channel._seats,
        Seat(
            label="xrev",
            runtime="claude-code",
            chat="-100888",
            project="alpha-engine",
            role=Role.REVIEWER,
            session="alpha-engine-xreview",
        ),
    ]
    runner.sessions["alpha-engine-xreview"] = SessionRef(
        "id-rev", "alpha-engine-xreview", str(repo), None, None
    )
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        checks={"proof": Path("NOTES/proof.md")},
        handoffs={name: Handoff(name=name, **spec) for name, spec in specs.items()},
    )
    channel._said_path = tmp_path / "last-said.json"
    last_said.remember(channel._kept(CHAT, SAID_FILE), chat_id=CHAT, text="The prompt for #355.")


def pressed_handoff(what: str, value: str) -> dict:
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data(what, value),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_a_bare_handoff_offers_each_handoff_as_a_button(tmp_path: Path, wired) -> None:
    """The shape `/checks` has: pick one, and it goes — or cancel."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    review = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}
    handoffs_in(channel, repo, tmp_path, runner, review=review, back={})

    await channel._run_handoff("", CHAT, None, f"tg:{APPROVER}")

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["review", "back"]
    assert rows[-1] == [cards.CANCEL]


async def test_review_reaches_the_reviewer_with_the_projects_text_in_front(
    tmp_path: Path, wired
) -> None:
    """The navigator's prompt, handed to the reviewer: the project's review text
    first, then the prompt itself — and no check, because none is named."""
    channel, _, runner, repo = wired
    review = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}
    handoffs_in(channel, repo, tmp_path, runner, review=review)

    await channel._handle_callback(pressed_handoff("handoff", "review"))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert "To xrev (reviewer), from Halyard — handoff: review." in text
    assert text.index("try to break it") < text.index("The prompt for #355.")
    assert "From: nav (navigator)" in text
    assert runner.asked == []


async def test_a_handoff_without_a_to_offers_every_seat(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, back={})

    await channel._run_handoff("back", CHAT, None, f"tg:{APPROVER}")

    keys = [key["text"] for key in api.sent[-1]["reply_markup"]["inline_keyboard"][0]]
    assert keys == ["→ nav", "→ xrev"]
    assert runner.sent == []


async def test_pressing_a_seat_hands_it_on_there(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, back={})

    await channel._handle_callback(pressed_handoff("handto", "back>xrev"))
    await settled(channel)

    [(session, _)] = runner.sent
    assert session == "id-rev"


#: A review handoff to the reviewer, with nothing else asked of it.
TO_THE_REVIEWER = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}


async def reviewed(channel) -> None:
    """Press the review handoff and wait for it to land, as a person would."""
    await channel._run_handoff("review", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)


async def test_a_handoff_counts_its_rounds_for_the_work_item(tmp_path: Path, wired) -> None:
    """The branch is 281-…, so both presses are rounds of alpha-engine#281's
    review: the first with the review text, the second with the followup."""
    channel, api, runner, repo = wired
    review = {
        "prompt": Path("NOTES/review.md"),
        "followup_prompt": Path("NOTES/review-followup.md"),
        "to": "reviewer",
    }
    handoffs_in(channel, repo, tmp_path, runner, review=review)
    (repo / "NOTES" / "review-followup.md").write_text("Only the earlier ENGELs.")

    await reviewed(channel)
    await reviewed(channel)

    first, second = (text for _, text in runner.sent)
    assert "- Round: 1/2" in first and "try to break it" in first
    assert "- Round: 2/2" in second and "Only the earlier ENGELs." in second
    assert "try to break it" not in second
    assert any("(round 2/2)" in sent["text"] for sent in api.sent)


async def test_the_second_round_carries_the_reviewers_answer_to_the_first(
    tmp_path: Path, wired
) -> None:
    from datetime import UTC, datetime

    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said

    channel, _, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, review=TO_THE_REVIEWER)

    await reviewed(channel)
    last_said.remember(
        channel._kept("-100888", SAID_FILE),
        chat_id="-100888",
        text="ENGEL: the loader skips a row.",
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    await reviewed(channel)

    second = runner.sent[-1][1]
    assert "xrev (reviewer)'s answer to round 1:" in second
    assert "ENGEL: the loader skips a row." in second


async def test_a_round_that_reached_nobody_is_not_counted(tmp_path: Path, wired) -> None:
    """Pressing again after a message went nowhere is the same round, not the
    next — otherwise a followup goes to a seat that never saw the first."""
    channel, _, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, review=TO_THE_REVIEWER)
    runner.accepting = False

    await reviewed(channel)
    runner.accepting = True
    await reviewed(channel)

    assert len(runner.sent) == 2
    assert all("- Round: 1/2" in text for _, text in runner.sent)


async def test_a_handoff_runs_its_checks_before_it_goes(tmp_path: Path, wired) -> None:
    """The reply and what the checks made of it arrive together, and the chat
    it was sent from is told which checks answered."""
    channel, api, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, discovery={"checks": ("proof",), "to": "xrev"})

    await channel._run_handoff("discovery delivery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert len(runner.asked) == 1
    [(_, text)] = runner.sent
    assert "Check proof — NOTES/proof.md" in text
    assert runner.says in text
    assert "Said by whoever asked: delivery" in text
    assert any("<b>proof</b>: answered" in sent["text"] for sent in api.sent)


class CheckAsking:
    """A check that asks to run a command half way through its turn, the way
    the gate brings one in — and, told to, waits on it until it is stopped."""

    def __init__(self, channel: TelegramChannel, runner: FakeRunner, *, waits: bool = False):
        self.channel = channel
        self.waits = waits
        #: How each turn was started, and the command each one asked for.
        self.started: list[dict] = []
        self.asked: list = []
        runner.ask = self.ask

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        from halyard.core.events import RiskLevel

        self.started.append(kwargs)
        request = await self.channel._store.create(
            session_id=kwargs["session_id"],
            agent_id="claude-code",
            project="alpha-engine",
            tool="Bash",
            command_summary="make test-fast",
            command_full="make test-fast",
            risk=RiskLevel.HIGH,
        )
        await self.channel.send_approval_request(request)
        self.asked.append(request)
        if self.waits:
            await asyncio.Event().wait()
        return "proof · no finding"

    async def until_asked(self) -> None:
        for _ in range(200):
            if self.asked:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the check never asked to run anything")


def stopping(request) -> dict:
    """Stop, pressed on a check's card by somebody allowed to."""
    from halyard.channels.telegram import cards

    return {
        "id": "cbq-1",
        "from": {"id": int(APPROVER)},
        "data": cards.callback_data(request, cards.STOP),
    }


async def test_a_command_a_check_asks_for_reaches_where_the_handoff_goes(
    tmp_path: Path, wired
) -> None:
    """The check's turn is nobody's seat. Its command is a card in the chat of
    the seat the handoff is for, saying which check and which handoff want it
    — and the turn stands in the project, with nothing that edits."""
    channel, api, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, discovery={"checks": ("proof",), "to": "xrev"})
    check = CheckAsking(channel, runner)

    await channel._run_handoff("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [how] = check.started
    assert how["cwd"] == repo
    assert how["edits"] is False
    [card] = [sent for sent in api.sent if "PERMISSION REQUEST" in sent["text"]]
    assert card["chat_id"] == "-100888"
    assert card["text"].startswith("<b>[CHECKER — PERMISSION REQUEST]</b>")
    assert "Check: <b>proof · handoff discovery</b>" in card["text"]
    keys = [key["text"] for row in card["reply_markup"]["inline_keyboard"] for key in row]
    assert keys[-1] == "⏹ Stop the check"
    assert channel._checking == {}


async def test_a_command_a_check_asks_for_here_is_carded_here(tmp_path: Path, wired) -> None:
    """`/checks` hands nothing on: the card says which check, in the chat that asked."""
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    CheckAsking(channel, runner)

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    [card] = [sent for sent in api.sent if "PERMISSION REQUEST" in sent["text"]]
    assert card["chat_id"] == CHAT
    assert "Check: <b>proof</b>" in card["text"]


async def test_stop_on_a_checks_card_ends_the_check_and_says_who(tmp_path: Path, wired) -> None:
    """Deny refuses one command and the check tries the next; Stop refuses it
    and ends the check. Measured: a claims check told only that it could run
    commands asked for one after another — the suite test by test, probes of
    its own, scratch copies — and nothing short of a restart could end it. The
    card says who stopped it, and the answer is an unmeasured one saying why."""
    from halyard.core.approvals import Decision

    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    check = CheckAsking(channel, runner, waits=True)

    await channel._handle_callback(pressed_check("proof"))
    await check.until_asked()
    [request] = check.asked
    await channel._handle_callback(stopping(request))
    await settled(channel)

    resolution = await channel._store.resolution_of(request.request_id)
    assert resolution.decision is Decision.DENY
    assert any(edit["text"].startswith(f"<b>⏹ STOPPED</b> by tg:{APPROVER}") for edit in api.edits)
    said = [sent["text"] for sent in api.sent]
    assert f"<b>proof</b> · unmeasured — stopped by tg:{APPROVER}" in said
    assert channel._checking == {}


async def test_a_handoff_goes_on_without_a_check_somebody_stopped(tmp_path: Path, wired) -> None:
    """Stop ends that check, not the handoff: the seat still gets the reply,
    with the stopped check said to be unmeasured and why."""
    channel, _, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, discovery={"checks": ("proof",), "to": "xrev"})
    check = CheckAsking(channel, runner, waits=True)

    handing = asyncio.ensure_future(channel._run_handoff("discovery", CHAT, None, f"tg:{APPROVER}"))
    await check.until_asked()
    await channel._handle_callback(stopping(check.asked[0]))
    await handing
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert f"stopped by tg:{APPROVER}" in text


async def test_a_check_is_told_whether_the_files_moved_since_the_reply(
    tmp_path: Path, wired, caplog
) -> None:
    """Where the files stood is kept as a reply comes in, and a check run on it
    later is told whether they still stand there. Measured: a check on a report
    three hours old, told nothing of it, set about rebuilding the tree."""
    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said

    caplog.set_level("INFO")
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    await channel.send_message(
        "id-nav",
        "All 42 tests passed.",
        Role.NAVIGATOR,
        agent_id="claude-code",
        project="alpha-engine",
    )
    said = last_said.last(channel._kept(CHAT, SAID_FILE), CHAT)
    (repo / "seed.txt").write_text("changed after the report\n")

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    assert said is not None and said.content
    [asked] = runner.asked
    assert "- At the reply: " in asked
    assert f"Content {said.content} · files changed since" in asked
    assert any(
        f"in alpha-engine: HEAD {said.head} · Content {said.content}" in record.getMessage()
        for record in caplog.records
    )


class Tracker:
    """A tracker that answers with a task's labels, or refuses."""

    name = "GitLab"

    def __init__(self, labels=(), refuse: Exception | None = None) -> None:
        self.on_task = tuple(labels)
        self.refuse = refuse
        self.asked: list[int] = []
        self.added: list[tuple[int, str]] = []

    async def task(self, number: int):
        from halyard.tasks.spec import Task

        self.asked.append(number)
        if self.refuse:
            raise self.refuse
        return Task(number=number, title="Rollout p3", labels=self.on_task)

    async def add_label(self, number: int, label: str):
        from halyard.tasks.spec import Task

        self.added.append((number, label))
        return Task(number=number, title="Rollout p3", labels=(*self.on_task, label))


def behind_a_tracker(channel: TelegramChannel, monkeypatch, tracker: Tracker) -> Tracker:
    """The project's remote, with this tracker behind it."""
    from halyard import tasks
    from halyard.channels.telegram import adapter as under_test

    monkeypatch.setattr(
        under_test.task_tracker,
        "origin_of",
        lambda path: tasks.Origin(host="gitlab.com", path="a/b"),
    )
    monkeypatch.setattr(under_test.task_tracker, "build", lambda *a, **k: tracker)
    channel._forge_token = "glpat-x"
    return tracker


def levels(channel: TelegramChannel) -> None:
    """A `level` label group on the project."""
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, label_groups={"level": ("level::1", "level::2", "level::3")}
    )


async def test_the_tasks_level_goes_on_the_envelope(tmp_path: Path, wired, monkeypatch) -> None:
    """Read from the tracker, one label from each of the project's groups, and
    put under the work item it belongs to."""
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    levels(channel)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("backend", "level::3")))

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    [asked] = runner.asked
    assert "- Work item: alpha-engine#281\n- level: level::3\n" in asked
    assert tracker.asked == [281]


async def test_a_tracker_that_cannot_be_read_leaves_the_envelope_as_it_was(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """This reports what is there; it is not a gate. A tracker refusing costs
    the line and nothing else — the check runs as it would have."""
    from halyard.tasks.spec import ForgeError

    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    levels(channel)
    behind_a_tracker(channel, monkeypatch, Tracker(refuse=ForgeError("GitLab says 401")))

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    [asked] = runner.asked
    assert "- Work item: alpha-engine#281" in asked
    assert "- level:" not in asked


async def test_a_project_without_label_groups_never_asks_the_tracker(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Not everybody needs this, and whoever does not never pays for it."""
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("level::3",)))

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    assert tracker.asked == []
    [asked] = runner.asked
    assert "- level:" not in asked


def findings_in(channel: TelegramChannel) -> None:
    """The project's checks say `status: candidate` when they found something."""
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, label_findings=("status: candidate",))


async def test_a_check_that_finds_something_labels_its_task(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Off to one side: the answer arrives as it always did, and the task gets
    the check's label."""
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    findings_in(channel)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("backend",)))
    runner.says = "proof · alpha-engine#281 · status: candidate"

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    assert tracker.added == [(281, "halyard:proof")]
    assert api.sent[-1]["text"].startswith("<b>proof</b>")


async def test_a_findings_label_already_on_the_task_is_not_written_again(
    tmp_path: Path, wired, monkeypatch
) -> None:
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    findings_in(channel)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("halyard:proof",)))
    runner.says = "proof · status: candidate"

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    assert tracker.added == []


def a_suite(channel: TelegramChannel, monkeypatch, *, output: str = "1420 passed") -> list:
    """`test-fast` among the project's commands, and a run of it that answers at once."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands={"test-fast": "make test-fast"})
    ran: list = []

    def running(line, path, *, timeout, on_progress=None):
        ran.append((line, path, timeout))
        return Result(ok=True, output=output, seconds=94.0, exit_code=0)

    monkeypatch.setattr(under_test.commands_running, "run", running)
    return ran


async def test_a_handoff_runs_its_commands_and_says_how_they_went(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Where the project is, one after another, and before the checks — then
    the seat gets what they did, and the chat is told."""
    channel, api, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"})
    ran = a_suite(channel, monkeypatch)

    await channel._run_handoff("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == [("make test-fast", repo, 600.0)]
    [(_, text)] = runner.sent
    assert "Ran test-fast: make test-fast · exit 0 · 94s · last line: 1420 passed" in text
    assert any("<b>test-fast</b>: passed" in sent["text"] for sent in api.sent)
    assert channel._working == {}


async def test_a_handoff_with_commands_does_not_go_while_another_runs(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Two `make` runs in one directory fight over the same outputs. Nothing
    is handed on, and it can be pressed again."""
    channel, api, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"})
    ran = a_suite(channel, monkeypatch)
    channel._working["alpha-engine"] = "test-all"

    await channel._run_handoff("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == []
    assert runner.sent == []
    assert "did not go" in api.sent[-1]["text"]
    assert channel._working == {"alpha-engine": "test-all"}


async def test_a_handoff_labels_like_a_check_run_by_hand(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """The same check, so the same label — handoffs are no exception."""
    channel, _, runner, repo = wired
    handoffs_in(channel, repo, tmp_path, runner, discovery={"checks": ("proof",), "to": "xrev"})
    findings_in(channel)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker())
    runner.says = "proof · status: candidate"

    await channel._run_handoff("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert tracker.added == [(281, "halyard:proof")]


async def test_pressing_a_check_runs_that_one_over_the_last_reply(tmp_path: Path, wired) -> None:
    """Its own instructions, the whole reply, and what Halyard can see."""
    from halyard.channels.telegram.adapter import CHECK_MODEL

    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof: find the evidence", claims="# claims")

    await channel._handle_callback(pressed_check("proof"))
    await settled(channel)

    [asked] = runner.asked
    assert asked.startswith("# proof: find the evidence")
    assert "All 42 tests passed." in asked
    assert "alpha-engine#281" in asked
    assert runner.models == [CHECK_MODEL]
    assert api.sent[-1]["text"].startswith("<b>proof</b>")


async def test_a_check_named_after_the_command_runs_with_the_note(tmp_path: Path, wired) -> None:
    """`/checks proof delivery` — the note says which stage the reply belongs to."""
    channel, _, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_message(typed("/checks proof delivery"))
    await settled(channel)

    [asked] = runner.asked
    assert "delivery" in asked


async def test_a_check_nobody_defined_is_said_and_the_rest_offered(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_checks("nope", CHAT, None)

    assert "no check called <b>nope</b>" in api.sent[-2]["text"]
    assert api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert runner.asked == []


async def test_a_check_the_model_could_not_answer_is_said_not_skipped(
    tmp_path: Path, wired
) -> None:
    """A missing answer reads exactly like a clean one."""
    channel, api, runner, repo = wired
    runner.says = None
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_checks("proof", CHAT, None)

    assert "unmeasured" in api.sent[-1]["text"]


async def test_a_project_without_checks_says_so(wired) -> None:
    channel, api, runner, _ = wired

    await channel._run_checks("", CHAT, None)

    assert "no <code>checks:</code>" in api.sent[-1]["text"]
    assert runner.asked == []


async def test_nothing_is_checked_before_anything_was_said(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")
    channel._said_path = tmp_path / "elsewhere" / "last-said.json"

    await channel._run_checks("", CHAT, None)

    assert "nothing to check" in api.sent[-1]["text"]
    assert runner.asked == []


async def test_checks_answers_from_a_phone(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    checks_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_message(typed("/checks"))
    await settled(channel)

    assert api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert runner.asked == []


def a_bare_remote(tmp_path: Path, repo: Path) -> Path:
    """Somewhere for a push to land, so the test exercises git rather than a
    double that would agree with whatever this file believes."""
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    return remote


async def test_committing_says_so_out_loud(wired) -> None:
    """A toast disappears and an edited card two screens up is easy to scroll
    past. The thing somebody needs to leave with is that it happened."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    said = api.sent[-1]["text"]
    assert "Committed" in said
    assert "281-power-gen-minor-fixes" in said
    assert "alpha-engine#281 loader stub and seed tweak" in said


async def test_commit_and_push_sends_the_branch(tmp_path: Path, wired) -> None:
    channel, api, _, repo = wired
    remote = a_bare_remote(tmp_path, repo)
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.SEND))

    assert "Pushed" in api.sent[-1]["text"]
    assert "origin/281-power-gen-minor-fixes" in api.sent[-1]["text"]
    landed = subprocess.run(
        ["git", "-C", str(remote), "log", "-1", "--format=%s", "281-power-gen-minor-fixes"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert landed == "alpha-engine#281 loader stub and seed tweak"


async def test_plain_commit_does_not_push(tmp_path: Path, wired) -> None:
    """Two buttons because the two undo differently."""
    channel, api, _, repo = wired
    remote = a_bare_remote(tmp_path, repo)
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert "Pushed" not in api.sent[-1]["text"]
    branches = subprocess.run(
        ["git", "-C", str(remote), "branch", "--list"], capture_output=True, text=True, check=True
    ).stdout
    assert branches.strip() == ""


async def test_a_push_that_fails_still_reports_the_commit(wired) -> None:
    """The commit is made and safe. Only the push failed, and conflating the
    two would send somebody looking for work that is already on disk."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))

    # No remote at all, so `git push` refuses.
    await tap(channel, press(only_handle(channel), commit_card.SEND))

    said = api.sent[-1]["text"]
    assert "Committed" in said
    assert "push failed" in said
    assert commit_count(repo) == 2


async def test_the_card_says_what_changed_not_only_which_files(wired) -> None:
    """The filenames say where an agent has been. This says what it did there,
    which is the question actually being answered by tapping Commit."""
    channel, api, runner, repo = wired
    runner.says = (
        "loader stub and seed tweak\n"
        "---\n"
        "- Adds a loader that returns the generated figure\n"
        "- Leaves the existing callers untouched\n"
    )
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    card = api.sent[-1]["text"]
    assert "Adds a loader that returns the generated figure" in card
    assert "Leaves the existing callers untouched" in card
    # The summary is for the card. The commit keeps this project's one-line
    # subjects rather than growing a body nobody's history has ever had.
    assert "alpha-engine#281 loader stub and seed tweak" in card
    await tap(channel, press(only_handle(channel), commit_card.MAKE))
    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"
    assert "Adds a loader" not in git(repo, "log", "-1", "--format=%B")


# --- what has to hold before a card is offered -------------------------------


def demands(channel: TelegramChannel, command: str | None) -> None:
    """Give the project a check to run, as `halyard.yaml` would: a line under
    `commands:`, and `validate:` naming it."""
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={**found.commands, "check": command} if command else found.commands,
        validate="check" if command else None,
    )


async def test_a_project_with_no_check_configured_runs_nothing(wired) -> None:
    """Absent means absent. A command invented on a project's behalf would fail
    on every commit."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert "Running" not in " ".join(m["text"] for m in api.sent)
    assert len(channel._proposals) == 1


async def test_a_failing_check_offers_no_card_at_all(wired) -> None:
    """A failing check is a fact, not a judgement — there is nothing here for
    somebody to weigh, so nothing is put in front of them."""
    channel, api, runner, repo = wired
    demands(channel, "echo 'FAIL: two tests broke' && exit 1")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert len(channel._proposals) == 0
    assert commit_count(repo) == 1
    assert "failed" in api.sent[-1]["text"]
    assert "FAIL: two tests broke" in api.sent[-1]["text"]
    # And the model was never asked, because the answer could not be used.
    assert runner.asked == []


async def test_it_says_the_check_is_running_before_it_starts(wired) -> None:
    """A project's own check can run for minutes, and silence for minutes reads
    as nothing having happened."""
    channel, api, _, repo = wired
    demands(channel, "true")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert "Running" in api.sent[0]["text"]
    assert "true" in api.sent[0]["text"]


async def test_a_passing_check_leads_to_an_ordinary_card(wired) -> None:
    channel, api, _, repo = wired
    demands(channel, "true")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert len(channel._proposals) == 1
    assert "alpha-engine#281" in api.sent[-1]["text"]


async def test_the_check_runs_in_the_project_and_sees_its_files(wired) -> None:
    channel, _, _, repo = wired
    demands(channel, "test -f loader.py")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert len(channel._proposals) == 1


async def test_a_task_id_missing_from_the_code_warns_without_blocking(wired) -> None:
    """A guess, treated like one. An agent that has lost the thread leaves
    references to its own conversation in the code; the branch's number showing
    up in what was written is a cheap sign it did not. A rename or a .gitignore
    fix will never mention it and is perfectly good, so this warns."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    card = api.sent[-1]["text"]
    assert "281 appears nowhere" in card
    assert card.index("281 appears nowhere") < card.index("COMMIT")  # above the heading
    assert len(channel._proposals) == 1

    await tap(channel, press(only_handle(channel), commit_card.MAKE))
    assert commit_count(repo) == 2


async def test_work_that_names_its_task_is_not_flagged(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "# alpha-engine#281 — the loader this task asked for\nx = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert "appears nowhere" not in api.sent[-1]["text"]


async def test_the_warning_survives_rewording_the_message(wired) -> None:
    """A warning that disappears when you type is a warning nobody heeds twice."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/review_and_commit"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))

    await deliver(channel, replying("power gen fixes", api.sent[-1]["text"]))

    assert "281 appears nowhere" in api.sent[-1]["text"]


async def test_a_project_can_turn_the_warnings_off(wired) -> None:
    """The task-id check is this project's house style, not a truth about
    software. Somebody who does not share it says `warn_if: []`."""
    channel, api, _, repo = wired
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, warn_if=())
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert "appears nowhere" not in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


async def test_a_warning_nobody_recognises_is_skipped_not_fatal(wired) -> None:
    """A typo in a list of opinions must not cost the ability to commit."""
    channel, _, _, repo = wired
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, warn_if=("no-such-check",))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert len(channel._proposals) == 1


# --- the round some work needs before it is committed ------------------------


def asks_a_round(channel: TelegramChannel, repo: Path, *, inquiry: str, review: str) -> None:
    """Give the project its two files, as `halyard.yaml` would."""
    from halyard.core.config_file import Confirmation

    notes = repo / "NOTES"
    notes.mkdir(exist_ok=True)
    (notes / "INQUIRY.md").write_text(inquiry)
    (notes / "REVIEW.md").write_text(review)
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        confirmation=Confirmation(inquiry=Path("NOTES/INQUIRY.md"), review=Path("NOTES/REVIEW.md")),
    )


async def test_a_project_without_a_round_is_offered_none(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    buttons = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert not any("Confirmation" in text for text in buttons)


async def test_the_round_is_offered_whether_or_not_the_model_flagged_it(wired) -> None:
    """The flag says where to look; whether to ask stays a person's call. They
    ask fishing questions on purpose, and those find bugs."""
    channel, api, _, repo = wired
    asks_a_round(channel, repo, inquiry="Raise a flag if...", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    buttons = {
        button["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row
    }
    assert any("Confirmation" in text for text in buttons)


async def test_the_project_own_question_reaches_the_model(wired) -> None:
    """Sent whole, because it is the project's writing about its own failures
    and summarising it would be Halyard having an opinion about a judgement
    that is deliberately not its own."""
    channel, _, runner, repo = wired
    asks_a_round(channel, repo, inquiry="RAISE IT WHEN A DATA FILE SHRINKS", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert "RAISE IT WHEN A DATA FILE SHRINKS" in runner.asked[0]


async def test_a_flag_is_shown_and_never_committed(wired) -> None:
    """The model is told to put it at the top of the message. Left there it
    would be committed — and the flag is a question asked *instead* of
    committing."""
    channel, api, runner, repo = wired
    asks_a_round(channel, repo, inquiry="ask", review="# The round")
    runner.says = (
        "⚠ worth a confirmation round — 6 records deleted from plants.json\n"
        "drop the null rows from the report\n"
        "---\n"
        "- Skips None entries\n"
    )
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))
    card = api.sent[-1]["text"]

    assert "6 records deleted from plants.json" in card
    assert "alpha-engine#281 drop the null rows from the report" in card

    await tap(channel, press(only_handle(channel), commit_card.MAKE))
    assert subject(repo) == "alpha-engine#281 drop the null rows from the report"
    assert "confirmation" not in git(repo, "log", "-1", "--format=%B")


async def test_pressing_the_round_sends_it_and_commits_nothing(wired) -> None:
    """The whole point. The work stays where it is, the navigator is asked, and
    committing afterwards is a fresh `/commit` that reads the branch again."""
    channel, api, runner, repo = wired
    asks_a_round(channel, repo, inquiry="ask", review="# Seven items of evidence")
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/review_and_commit"))

    await tap(channel, press(only_handle(channel), commit_card.CONFIRM))

    assert commit_count(repo) == 1
    assert len(channel._proposals) == 0
    await asyncio.sleep(0.05)
    # Into the navigator's session, not into this chat: the round is work for an
    # agent, and it stays readable in that seat's own conversation.
    assert any("Seven items of evidence" in text for _, text in runner.sent)
    assert "CONFIRMATION ROUND" in api.edits[-1]["text"]


async def test_the_round_goes_to_the_navigator_not_to_whoever_asked(wired) -> None:
    channel, api, runner, repo = wired
    channel._seats = [
        Seat(
            label="nav",
            runtime="claude-code",
            chat=CHAT,
            project="alpha-engine",
            role=Role.NAVIGATOR,
            session="alpha-engine-navigator",
        ),
        Seat(
            label="drv",
            runtime="claude-code",
            chat=CHAT,
            project="alpha-engine",
            role=Role.DRIVER,
            session="alpha-engine-driver",
        ),
    ]
    asks_a_round(channel, repo, inquiry="ask", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/review_and_commit"))

    await tap(channel, press(only_handle(channel), commit_card.CONFIRM))
    await asyncio.sleep(0.05)

    # The navigator's own session, resolved by the runtime — not the driver's,
    # and not the chat the command was typed in.
    assert [session for session, _ in runner.sent] == ["id-nav"]
    assert "nav" in api.edits[-1]["text"]


async def test_a_project_with_no_navigator_says_so(wired) -> None:
    channel, api, _, repo = wired
    channel._seats = [
        Seat(
            label="drv", runtime="claude-code", chat=CHAT, project="alpha-engine", role=Role.DRIVER
        )
    ]
    asks_a_round(channel, repo, inquiry="ask", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/review_and_commit"))

    await tap(channel, press(only_handle(channel), commit_card.CONFIRM))

    assert "no navigator" in api.sent[-1]["text"]
    assert commit_count(repo) == 1


async def test_a_round_file_that_is_not_there_says_so_rather_than_committing(wired) -> None:
    from halyard.core.config_file import Confirmation

    channel, api, _, repo = wired
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, confirmation=Confirmation(review=Path("NOTES/GONE.md"))
    )
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/review_and_commit"))

    await tap(channel, press(only_handle(channel), commit_card.CONFIRM))

    assert "no confirmation round" in api.sent[-1]["text"]
    assert commit_count(repo) == 1


async def test_a_slow_commit_does_not_stop_anything_else_being_answered(wired) -> None:
    """The defect this was built for.

    Updates are handled one at a time in the loop that fetches them, so a
    `/commit` running a project's test suite parked the poller for minutes.
    Cards kept arriving — those are sent from the HTTP side — and not one of
    them could be answered. From a phone that is indistinguishable from Halyard
    being down, and worse, because the approvals expire while it looks alive.
    """
    channel, api, _, repo = wired
    demands(channel, "sleep 5")
    wrote(repo, "loader.py", "x = 1\n")

    # Not awaited: this is the poll loop handing the update over and moving on.
    await channel._handle_message(typed("/review_and_commit"))

    # And the very next update is answered while that is still going.
    await channel._handle_message(typed("/status"))

    assert any("seat" in m["text"].lower() or "nav" in m["text"] for m in api.sent[-2:])
    assert [task for task in channel._sending if not task.done()], "the commit finished too soon"
    for task in list(channel._sending):
        task.cancel()


# --- two commands, one path --------------------------------------------------


async def test_a_plain_commit_runs_no_checks_at_all(wired) -> None:
    """What most changes want, and what a change to `scripts/` wants every time.

    Split by command rather than by a list of exceptions: a rule naming the
    paths that skip the gate has to be maintained against a repository that
    keeps growing, and gets it wrong quietly. A second command is chosen at the
    moment somebody already knows which of the two they meant.
    """
    channel, api, _, repo = wired
    demands(channel, "echo ran >> " + str(repo / "ran.txt"))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert not (repo / "ran.txt").exists()
    assert "Running" not in " ".join(m["text"] for m in api.sent)
    assert len(channel._proposals) == 1


async def test_a_plain_commit_still_proposes_and_commits(wired) -> None:
    channel, _, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))
    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"


async def test_a_plain_commit_carries_no_warnings_and_no_round(wired) -> None:
    """The warnings and the round belong to the command that asked for them."""
    channel, api, _, repo = wired
    asks_a_round(channel, repo, inquiry="ask", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    card = api.sent[-1]
    assert "appears nowhere" not in card["text"]
    buttons = {b["text"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
    assert not any("Confirmation" in one for one in buttons)


async def test_rewording_a_plain_commit_does_not_grow_a_round_button(wired) -> None:
    """Which card offers the round is a fact about the command that made it."""
    channel, api, _, repo = wired
    asks_a_round(channel, repo, inquiry="ask", review="# The round")
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))

    await deliver(channel, replying("a better subject", api.sent[-1]["text"]))

    buttons = {b["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
    assert not any("Confirmation" in one for one in buttons)


def test_both_commands_are_registered_and_telegram_will_accept_them() -> None:
    """One invalid name and Telegram rejects the whole list, so every command
    is silently missing from the menu."""
    from halyard.channels.telegram.adapter import COMMANDS

    names = {name for name, _ in COMMANDS}
    assert {"commit", "review_and_commit"} <= names
    for name, description in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert len(description) <= 256, name
