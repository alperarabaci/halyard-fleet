"""Tests for `/commit` — the Telegram half.

`tests/test_commit_repository.py` covers what git is asked and how its answers are read.
This covers the part that can commit something nobody agreed to: which button
does it, who is allowed to press it, and what happens when it is pressed twice.

The repository is real, so a passing test here means a commit actually landed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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

    async def get_updates(self, *, offset=None, timeout=30) -> list[dict]:
        await asyncio.sleep(0.01)
        return []


class FakeRunner:
    """Writes a fixed subject line, and remembers what it was asked."""

    id = "claude-code"
    available = True

    def __init__(self, *, says: str | None = "loader stub and seed tweak") -> None:
        self.says = says
        self.asked: list[str] = []
        self.models: list[str | None] = []
        #: The system prompt each turn asked for in place of Claude Code's own.
        self.systems: list[str | None] = []
        #: How hard each one-shot turn was asked to think.
        self.efforts: list[str | None] = []
        #: The id each one-shot turn ran under — what its tokens are recorded by.
        self.session_ids: list[str | None] = []
        self.sent: list[tuple[str, str]] = []
        #: Session names this runtime claims to know, as the real one would.
        self.sessions: dict[str, object] = {}
        #: Whether a session takes what is sent to it, as `send` answers.
        self.accepting = True
        #: What happens between a message going in and `send` answering, when a
        #: test says: a turn queued behind another, or one that answers fast.
        self.turn = None
        #: How the last turn sent can say it ended badly after it was accepted,
        #: as a real runtime does minutes later.
        self.when_done = None

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        self.asked.append(text)
        self.models.append(model)
        self.efforts.append(kwargs.get("effort"))
        self.systems.append(kwargs.get("system"))
        self.session_ids.append(kwargs.get("session_id"))
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
        started=None,
    ) -> bool:
        """What reaches a session, as against what reaches the chat."""
        self.sent.append((session_id, text))
        self.when_done = when_done
        if self.turn is not None:
            return await self.turn(session_id, started)
        if started is not None and await started(session_id) is False:
            return False
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


async def test_the_message_is_written_on_the_default_model_halyard_is_given(wired) -> None:
    """`HALYARD_DEFAULT_MODEL` and `HALYARD_DEFAULT_EFFORT`, so the commit
    message's model is a line in halyard.yaml rather than a constant."""
    from halyard.core.config_file import ModelChoice

    channel, _, runner, repo = wired
    channel._default_model = ModelChoice("haiku", "low")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert (runner.models, runner.efforts) == (["haiku"], ["low"])


async def test_another_agent_s_chat_has_its_message_written_where_the_model_runs(wired) -> None:
    """The model says the runtime, not the chat: `sonnet` is the default
    runtime's word, so an opencode chat's message is written there — where it
    used to keep the reference alone, rather than hand opencode `sonnet`."""
    channel, _, runner, repo = wired
    other = FakeRunner()
    channel._runners["opencode"] = other
    channel._seats = [replace(seat, runtime="opencode") for seat in channel._seats]
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert other.asked == []
    assert runner.models == ["sonnet"]


async def test_a_gpt_default_model_writes_the_message_on_codex(wired) -> None:
    """`HALYARD_DEFAULT_MODEL: gpt-5.6-terra` is all it takes: no Claude turn."""
    from halyard.core.config_file import ModelChoice

    channel, _, runner, repo = wired
    codex = FakeRunner()
    channel._runners["codex"] = codex
    channel._default_model = ModelChoice("gpt-5.6-terra", "high")
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert (codex.models, codex.efforts) == (["gpt-5.6-terra"], ["high"])
    assert runner.asked == []


async def test_commit_override_selects_its_runtime_without_changing_other_choices(wired):
    from halyard.core.config_file import ModelChoice

    channel, _, runner, repo = wired
    codex = FakeRunner()
    channel._runners["codex"] = codex
    channel._default_model = ModelChoice("sonnet", "high")
    channel._commit_model = ModelChoice("gpt-6-luna", "low")
    inspection = channel._inspection_model
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert (codex.models, codex.efforts) == (["gpt-6-luna"], ["low"])
    assert runner.asked == []
    assert channel._default_model == ModelChoice("sonnet", "high")
    assert channel._inspection_model == inspection
    assert commit_count(repo) == 1


async def test_the_message_is_written_with_nothing_but_the_diff(wired) -> None:
    """Claude Code's own prompt and tools were five sixths of what a message
    cost, and the diff in front of it is all it needs."""
    from halyard import commits

    channel, _, runner, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert runner.systems == [commits.SYSTEM]


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
        assert f"no agent has it. Give an agent <code>chat: {CHAT}</code>" in said
    finally:
        await audit.close()


async def test_a_chat_whose_project_has_no_path_says_whose_chat_it_is(wired) -> None:
    """The seat is the part nobody can see from the chat, and it can be one
    nobody remembers setting up."""
    channel, api, _, _ = wired
    # A project written without a `path:` never reaches the channel.
    channel._repositories.clear()

    await deliver(channel, typed("/transition"))

    assert (
        "it is <b>nav</b>'s, and nav's project <b>alpha-engine</b> has no <code>path:</code>"
        in api.sent[-1]["text"]
    )


async def test_a_chat_whose_seat_is_in_no_project_says_so(wired) -> None:
    channel, api, _, _ = wired
    channel._seats = [replace(seat, project=None) for seat in channel._seats]
    # With a single project every chat is about it, so there is none here.
    channel._repositories.clear()

    await deliver(channel, typed("/inspect"))

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


async def test_a_reworded_card_takes_the_place_of_the_one_before(wired) -> None:
    """Left open, the old card's Commit committed the new wording under the old
    one's text — a card that says one thing and does another."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    [first] = channel._desk.of("commit", handle)
    await tap(channel, press(handle, commit_card.REWRITE, message_id=first.message_id))

    await deliver(channel, replying("power gen minor fixes", api.sent[-1]["text"]))

    [now] = channel._desk.of("commit", handle)
    assert now.message_id != first.message_id
    [closed] = [edit for edit in api.edits if edit["message_id"] == first.message_id]
    assert "Reworded" in closed["text"]


async def test_a_card_git_refused_says_nothing_was_committed(wired) -> None:
    """The proposal is spent by the press, so its card says so instead of
    offering a Commit that can only answer that the commit is no longer open."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    [card] = channel._desk.of("commit", handle)
    (repo / "loader.py").unlink()

    await tap(channel, press(handle, commit_card.MAKE, message_id=card.message_id))

    [closed] = [edit for edit in api.edits if edit["message_id"] == card.message_id]
    assert "NOT COMMITTED" in closed["text"]
    assert channel._desk.of("commit", handle) == []


async def test_a_proposal_left_too_long_closes_its_card(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    [card] = channel._desk.of("commit", only_handle(channel))
    assert card.expires_at is not None, "good until the proposal goes stale"
    card.expires_at = channel._clock() - timedelta(seconds=1)

    await channel._close_expired()

    [closed] = [edit for edit in api.edits if edit["message_id"] == card.message_id]
    assert "EXPIRED" in closed["text"]


async def test_a_press_on_a_proposal_that_is_gone_closes_its_card(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    handle = only_handle(channel)
    [card] = channel._desk.of("commit", handle)
    channel._proposals._open.clear()

    await tap(channel, press(handle, commit_card.MAKE, message_id=card.message_id))

    assert api.answers[-1]["text"] == "That commit is no longer open."
    [closed] = [edit for edit in api.edits if edit["message_id"] == card.message_id]
    assert "No longer open" in closed["text"]


# --- the command itself -----------------------------------------------------


def test_commit_is_registered_so_it_appears_when_you_type_a_slash() -> None:
    from halyard.channels.telegram.adapter import COMMANDS

    assert ("commit", "Commit this branch's work, plainly or after this project's own checks") in (
        COMMANDS
    )


# --- saying it happened, and pushing ----------------------------------------


# --- /inspect: a project's own inspections over the last reply ---------------
#
# Here because it runs on the same two things `/commit` does: the project's
# repository, and a one-shot model turn.


def inspections_in(channel: TelegramChannel, repo: Path, tmp_path: Path, **texts: str) -> None:
    """Give the project inspections on disk, and a reply in the chat to inspect."""
    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said

    (repo / "NOTES").mkdir(exist_ok=True)
    for name, text in texts.items():
        (repo / "NOTES" / f"{name}.md").write_text(text)
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, inspections={name: Path(f"NOTES/{name}.md") for name in texts}
    )
    channel._said_path = tmp_path / "last-said.json"
    last_said.remember(channel._kept(CHAT, SAID_FILE), chat_id=CHAT, text="All 42 tests passed.")


def pressed_inspection(name: str, what: str = "inspect") -> dict:
    """The button an inspection is offered on, pressed by somebody allowed to."""
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data(what, name),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_a_bare_checks_offers_each_check_as_a_button(tmp_path: Path, wired) -> None:
    """The shape `/label` and `/command` already have: pick one, and it runs —
    or cancel, and nothing does."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof", claims="# claims")

    await channel._run_inspection("", CHAT, None)

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["proof", "claims"]
    assert rows[-1] == [cards.CANCEL]
    assert runner.asked == []


async def test_cancelling_a_choice_card_takes_its_buttons_away(tmp_path: Path, wired) -> None:
    """The card stays, so the chat still shows what was offered; the buttons go,
    so nothing on it can be pressed by mistake later. Nothing runs."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

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
    inspections_in(channel, repo, tmp_path, proof="# proof")
    caplog.set_level(logging.INFO)

    await channel._run_inspection("proof delivery", CHAT, None)

    asked = next(
        r.getMessage() for r in caplog.records if "Inspection proof asked" in r.getMessage()
    )
    assert "alpha-engine#281" in asked
    assert "NOTES/proof.md @ uncommitted" in asked
    assert "'delivery'" in asked
    assert "Inspection proof answered" in caplog.text
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
    inspections_in(channel, repo, tmp_path, proof="# proof")
    last_said.remember(
        channel._kept(CHAT, SAID_FILE),
        chat_id=CHAT,
        text="done",
        now=datetime(2026, 9, 13, 20, 20, tzinfo=UTC),
    )
    monkeypatch.setenv("TZ", "Europe/Istanbul")
    time.tzset()
    try:
        await channel._run_inspection("", CHAT, None)
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
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_inspection("proof", CHAT, None)

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "→ nav"
    assert rows[-1] == [cards.CANCEL]


async def test_a_seats_button_hands_it_the_whole_answer_and_what_it_is(
    tmp_path: Path, wired
) -> None:
    """With the line that says what it is, so the session can tell a finding
    from an instruction."""
    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    await channel._run_inspection("proof", CHAT, None)

    await channel._handle_callback(pressed_result("proof>nav"))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-nav"
    assert "To nav (navigator), from Halyard." in text
    assert "Inspection: proof — NOTES/proof.md @ uncommitted" in text
    assert "alpha-engine#281" in text
    # The findings, then the reply they are about: a finding about a report the
    # reader does not have is a finding nobody can weigh.
    assert text.index(runner.says) < text.index("All 42 tests passed.")


async def test_the_button_under_one_check_sends_that_checks_answer(tmp_path: Path, wired) -> None:
    """A chat holds several checks' answers; the one under `proof` sends proof's."""
    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof", claims="# claims")
    runner.says = "proof found this"
    await channel._run_inspection("proof", CHAT, None)
    runner.says = "claims found that"
    await channel._run_inspection("claims", CHAT, None)

    await channel._handle_callback(pressed_result("proof>nav"))
    await settled(channel)

    [(_, text)] = runner.sent
    assert "proof found this" in text
    assert "claims found that" not in text


async def test_an_answer_no_longer_kept_says_so(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

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


# --- /transition: a reply handed on the way the project defines it ---------------


def transitions_in(channel, repo: Path, tmp_path: Path, runner, **specs: dict) -> None:
    """A reviewer seat, a check, the transitions asked for, and a reply to hand on."""
    from halyard.channels.telegram.adapter import SAID_FILE
    from halyard.core import last_said
    from halyard.core.config_file import Transition

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
        inspections={"proof": Path("NOTES/proof.md")},
        transitions={name: Transition(name=name, **spec) for name, spec in specs.items()},
    )
    channel._said_path = tmp_path / "last-said.json"
    last_said.remember(channel._kept(CHAT, SAID_FILE), chat_id=CHAT, text="The prompt for #355.")


def pressed_transition(what: str, value: str) -> dict:
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data(what, value),
        "message": {"message_id": 5, "chat": {"id": CHAT}},
    }


async def test_a_bare_transition_offers_each_transition_as_a_button(tmp_path: Path, wired) -> None:
    """The shape `/checks` has: pick one, and it goes — or cancel."""
    from halyard.channels.telegram import cards

    channel, api, runner, repo = wired
    review = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}
    transitions_in(channel, repo, tmp_path, runner, review=review, back={})

    await channel._run_transition("", CHAT, None, f"tg:{APPROVER}")

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
    transitions_in(channel, repo, tmp_path, runner, review=review)

    await channel._handle_callback(pressed_transition("handoff", "review"))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert "To xrev (reviewer), from Halyard — transition: review." in text
    assert text.index("try to break it") < text.index("The prompt for #355.")
    assert "From: nav (navigator)" in text
    assert runner.asked == []


@pytest.mark.parametrize("command", ["/transition review", "/handoff review"])
async def test_a_transition_named_after_the_command_goes_and_the_old_name_still_answers(
    tmp_path: Path, wired, command: str
) -> None:
    """`/handoff` is what `/transition` was called until 2026-09-24: typed, it
    still does what it did."""
    channel, _, runner, repo = wired
    review = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}
    transitions_in(channel, repo, tmp_path, runner, review=review)

    await channel._handle_message(typed(command))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert "To xrev (reviewer), from Halyard — transition: review." in text


async def test_a_transition_without_a_to_offers_every_seat(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    transitions_in(channel, repo, tmp_path, runner, back={})

    await channel._run_transition("back", CHAT, None, f"tg:{APPROVER}")

    keys = [key["text"] for key in api.sent[-1]["reply_markup"]["inline_keyboard"][0]]
    assert keys == ["→ nav", "→ xrev"]
    assert runner.sent == []


def with_drivers(channel, repo: Path, runner) -> None:
    """Two drivers on the project, as a machine with one per runtime has."""
    for label, session, chat in (
        ("drv", "alpha-engine-driver", "-100901"),
        ("xdrv", "alpha-engine-xdriver", "-100902"),
    ):
        channel._seats.append(
            Seat(
                label=label,
                runtime="claude-code",
                chat=chat,
                project="alpha-engine",
                role=Role.DRIVER,
                session=session,
            )
        )
        runner.sessions[session] = SessionRef(f"id-{label}", session, str(repo), None, None)


def first_row(api) -> list[tuple[str, str | None]]:
    return [
        (key["text"], key.get("style"))
        for key in api.sent[-1]["reply_markup"]["inline_keyboard"][0]
    ]


async def test_a_transition_offers_the_driver_last_worked_with_first(tmp_path: Path, wired) -> None:
    """Whichever way the work went: its reply counts, from the desk or not."""
    channel, api, runner, repo = wired
    transitions_in(channel, repo, tmp_path, runner, develop={"to": "driver"})
    with_drivers(channel, repo, runner)

    await channel.send_message(
        "id-xdrv", "Done.", agent_id="claude-code", session_name="alpha-engine-xdriver"
    )
    await channel._run_transition("develop", CHAT, None, f"tg:{APPROVER}")

    assert first_row(api) == [("→ xdrv", "primary")]
    assert runner.sent == [], "offered first, not chosen"


async def test_work_handed_to_an_agent_makes_it_the_first_offered(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    transitions_in(channel, repo, tmp_path, runner, develop={"to": "driver"})
    with_drivers(channel, repo, runner)
    await channel.send_message(
        "id-xdrv", "Done.", agent_id="claude-code", session_name="alpha-engine-xdriver"
    )

    await channel._handle_message(typed("/to drv start on the loader"))
    await settled(channel)
    await channel._run_transition("develop", CHAT, None, f"tg:{APPROVER}")

    assert first_row(api) == [("→ drv", "primary")]


async def test_forward_offers_the_project_s_last_agents_first_and_not_its_own(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    transitions_in(channel, repo, tmp_path, runner)
    with_drivers(channel, repo, runner)
    await channel.send_message(
        "id-xdrv", "Done.", agent_id="claude-code", session_name="alpha-engine-xdriver"
    )

    await channel._handle_message(typed("/forward"))

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    keys = [key["text"] for row in rows for key in row]
    assert first_row(api) == [("xdrv", "primary")]
    assert "nav" not in keys, "this chat's own agent is not offered"


async def test_pressing_a_seat_hands_it_on_there(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    transitions_in(channel, repo, tmp_path, runner, back={})

    await channel._handle_callback(pressed_transition("handto", "back>xrev"))
    await settled(channel)

    [(session, _)] = runner.sent
    assert session == "id-rev"
    assert {"message_id": 5, "text": "☑️ back → xrev"} in api.edits, "the card is closed"


async def test_the_card_a_transition_is_chosen_on_closes(tmp_path: Path, wired) -> None:
    """Spent once it is used: its text stays and says which was chosen, and its
    buttons go, so the same transition cannot be sent from it again by mistake."""
    channel, api, runner, repo = wired
    transitions_in(
        channel,
        repo,
        tmp_path,
        runner,
        review={"prompt": Path("NOTES/review.md"), "to": "reviewer"},
    )
    pressed = pressed_transition("handoff", "review")
    pressed["message"]["text"] = "Hand the last reply here on how?"

    await channel._handle_callback(pressed)
    await settled(channel)

    closed = {"message_id": 5, "text": "Hand the last reply here on how?\n\n☑️ review"}
    assert closed in api.edits
    assert runner.sent, "and the transition went"


#: A review transition to the reviewer, with nothing else asked of it.
TO_THE_REVIEWER = {"prompt": Path("NOTES/review.md"), "to": "reviewer"}


async def reviewed(channel) -> None:
    """Press the review transition and wait for it to land, as a person would."""
    await channel._run_transition("review", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)


async def test_a_transition_pressed_by_hand_counts_no_rounds(tmp_path: Path, wired) -> None:
    """Rounds are a workflow's. Pressed twice by hand, a transition sends its own
    prompt twice and says no round: a `1/2` where there is no second step would
    be a workflow's word where there is no workflow."""
    channel, api, runner, repo = wired
    review = {
        "prompt": Path("NOTES/review.md"),
        "followup_prompt": Path("NOTES/review-followup.md"),
        "to": "reviewer",
    }
    transitions_in(channel, repo, tmp_path, runner, review=review)
    (repo / "NOTES" / "review-followup.md").write_text("Only the earlier BLOCKERs.")

    await reviewed(channel)
    await reviewed(channel)

    first, second = (text for _, text in runner.sent)
    assert "try to break it" in first and "try to break it" in second
    assert "Only the earlier BLOCKERs." not in second
    assert not any("Round:" in text for _, text in runner.sent)
    assert not any("(round" in sent["text"] for sent in api.sent)


# --- /workflow: the transitions taken in the order the project wrote them down ---


def flow_in(
    channel,
    repo: Path,
    tmp_path: Path,
    runner,
    *,
    flows: dict,
    steps: dict,
    decisions: dict | None = None,
    stretches: dict | None = None,
    phases: int = 3,
) -> None:
    """Two transitions, the steps a flow takes them in, and any words and phases
    of its own."""
    from halyard.core.config_file import Decisions, Step, Workflows

    transitions_in(
        channel,
        repo,
        tmp_path,
        runner,
        review={"prompt": Path("NOTES/review.md"), "to": "reviewer"},
        to_nav={"prompt": Path("NOTES/review.md"), "to": "navigator"},
    )
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        workflows=Workflows(
            steps={name: Step(name=name, **spec) for name, spec in steps.items()},
            decisions=Decisions(**(decisions or {})),
            flows={name: tuple(order) for name, order in flows.items()},
            stretches=stretches or {},
            phases=phases,
        ),
    )


#: A flow of two steps: the reviewer, which the work may come back to once, then
#: the navigator.
TWO_STEPS = {
    "flows": {"level3": ["review", "to_nav"]},
    "steps": {
        "review": {"transition": "review", "seat": "xrev", "rounds": 2},
        "to_nav": {"transition": "to_nav", "seat": "nav"},
    },
}

SESSIONS = {
    "xrev": ("id-rev", "alpha-engine-xreview"),
    "nav": ("id-nav", "alpha-engine-navigator"),
}


async def started(channel, at: int = 0) -> None:
    """Press the workflow and a step of it, and wait for that step to land."""
    await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=at)
    await settled(channel)


async def answered(channel, seat: str, text: str) -> None:
    """A seat's reply arriving the way the relay brings it."""
    session_id, name = SESSIONS[seat]
    await channel.send_message(
        session_id,
        text,
        agent_id="claude-code",
        session_name=name,
        project="alpha-engine",
    )
    await settled(channel)


async def test_a_bare_workflow_offers_each_one(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")

    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["level3"]


async def test_a_workflow_pressed_asks_which_step_to_start_at(tmp_path: Path, wired) -> None:
    """The work is often past the first step already."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await channel._handle_callback(pressed_transition("flow", "level3"))
    await settled(channel)

    assert runner.sent == []
    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["1 · review", "2 · to_nav"]


async def test_a_step_pressed_starts_the_workflow_there_with_the_chat_s_last_reply(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await channel._handle_callback(pressed_transition("flowat", "level3>1"))
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-nav"
    assert "- Workflow: level3 · step 2 of 2 · to_nav" in text
    assert "The prompt for #355." in text


async def test_a_step_can_be_named_when_the_command_is_typed(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await channel._run_workflow("level3 gone", CHAT, None, f"tg:{APPROVER}")
    offered = api.sent[-1]
    await channel._run_workflow("level3 to_nav", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert any("no step called <b>gone</b>" in sent["text"] for sent in api.sent)
    assert offered["reply_markup"]["inline_keyboard"][0][0]["text"] == "1 · review"
    assert [session for session, _ in runner.sent] == ["id-nav"]


async def test_a_workflow_starts_at_its_first_step(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await started(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert "- Workflow: level3 · step 1 of 2 · review" in text
    assert (
        "- Decide on your last line: forward (→ to_nav, nav) · back (→ the operator) "
        "· wait (→ the operator)"
    ) in text
    assert any("level3</b> 1/2" in sent["text"] for sent in api.sent)


async def test_a_project_s_own_words_move_the_run_and_are_the_ones_it_is_told(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS, decisions={"forward": "go"})
    await started(channel)

    assert "- Decide on your last line: go (→ to_nav, nav)" in runner.sent[0][1]

    await answered(channel, "xrev", "Nothing blocking.\nRESULT: go")

    assert runner.sent[-1][0] == "id-nav"


async def test_a_transition_pressed_by_hand_says_nothing_of_a_workflow(
    tmp_path: Path, wired
) -> None:
    """The lines are the workflow's, and a transition pressed by hand has none."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await channel._run_transition("review", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [(_, text)] = runner.sent
    assert "Workflow:" not in text
    assert "Decide on your last line" not in text


async def test_the_word_for_forward_takes_the_next_step_with_the_reply(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await answered(channel, "xrev", "Nothing blocking.\nDECISION: forward")

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "Nothing blocking." in text
    assert "- Workflow: level3 · step 2 of 2 · to_nav" in text


async def test_back_returns_to_the_step_before_and_says_who_sent_it(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "The loader skips a row.\nDECISION: back")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "Sent back by: nav (navigator)" in text
    assert "- Round: 2/2" in text, "the step before goes round again"


#: The reviewer reports, the navigator says it can close, and the close
#: works on the reviewer's report rather than the navigator's word on it.
WORKED_ON = {
    "flows": {"level3": ["review", "to_nav", "close"]},
    "steps": {
        "review": {"transition": "review", "seat": "xrev", "rounds": 2},
        "to_nav": {"transition": "to_nav", "seat": "nav"},
        "close": {"transition": "to_nav", "seat": "nav", "rounds": 2, "works_on": "review"},
    },
}


async def test_a_step_that_works_on_another_carries_that_agent_s_last_reply(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **WORKED_ON)
    await started(channel)
    await answered(channel, "xrev", "Report: the loader is fixed.\nDECISION: forward")

    await answered(channel, "nav", "This can be closed.\nDECISION: forward")

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "- Workflow: level3 · step 3 of 3 · close" in text
    assert "Report: the loader is fixed." in text
    assert "This can be closed." not in text


async def test_back_from_a_step_that_works_on_another_goes_to_that_agent(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **WORKED_ON)
    await started(channel)
    await answered(channel, "xrev", "Report: the loader is fixed.\nDECISION: forward")
    await answered(channel, "nav", "This can be closed.\nDECISION: forward")

    await answered(channel, "nav", "The proof misses a case.\nDECISION: back")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "The proof misses a case." in text, "sent back to, it carries what sent it back"
    assert "- Round: 2/2" in text


def keys_of(sent: dict) -> list[str]:
    return [key["text"] for row in sent["reply_markup"]["inline_keyboard"] for key in row]


async def test_a_reply_that_decides_nothing_stops_and_offers_where_its_words_go(
    tmp_path: Path, wired
) -> None:
    """Carried on, it handed the next agent a reply never meant for it."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await answered(channel, "xrev", "All 42 tests pass.")

    assert [session for session, _ in runner.sent] == ["id-rev"], "nothing went on"
    stopped = api.sent[-1]
    assert "review ended with no decision" in stopped["text"]
    assert "Or answer <b>xrev</b> in its own chat" in stopped["text"]
    # Back from the first step goes nowhere, so only forward is offered; stop is last.
    assert keys_of(stopped) == ["▶️ forward: to_nav → nav", "🧭 Pick a step", "⏹ Stop the workflow"]


async def test_an_offer_sends_that_step_with_the_agent_s_reply(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "All 42 tests pass.")

    await channel._handle_callback(pressed_transition("flowto", "level3>1>f"))
    await settled(channel)

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "All 42 tests pass." in text


async def test_the_agent_s_own_decision_after_a_stop_moves_the_run(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "All 42 tests pass.")

    await answered(channel, "xrev", "Checked again.\nDECISION: forward")

    assert runner.sent[-1][0] == "id-nav"


async def test_after_a_wait_the_card_names_what_it_would_send(tmp_path: Path, wired) -> None:
    """After a wait, what is ready is the step after the one that waited. A
    button that said "send it anyway" sent the navigator a driver's question to
    somebody who meant "carry on"."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await answered(channel, "xrev", "Which schema do you mean?\nDECISION: wait")

    keys = keys_of(api.sent[-1])
    assert keys[0] == "▶️ forward: to_nav → nav"
    assert "▶️ Send it anyway" not in keys
    assert keys[-1] == "⏹ Stop the workflow"


#: A review that may go once, and a navigator that may go twice: the
#: navigator's back runs into the review's round limit.
ONE_REVIEW = {
    "flows": {"level3": ["review", "to_nav"]},
    "steps": {
        "review": {"transition": "review", "seat": "xrev", "rounds": 1},
        "to_nav": {"transition": "to_nav", "seat": "nav", "rounds": 2},
    },
}


async def stopped_on_the_round_limit(channel) -> None:
    await started(channel)
    await answered(channel, "xrev", "Reviewed.\nDECISION: forward")
    await answered(channel, "nav", "Not yet.\nDECISION: back")


async def test_workflow_shows_a_stopped_run_the_card_its_stop_showed(tmp_path: Path, wired) -> None:
    """Asked again later, the card offered only the round it had stopped on
    and Stop: nothing to send the run on with."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **ONE_REVIEW)
    await stopped_on_the_round_limit(channel)
    at_the_stop = keys_of(api.sent[-1])

    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")

    assert "review would go for round 2 of 1" in api.sent[-1]["text"]
    assert keys_of(api.sent[-1]) == at_the_stop
    assert keys_of(api.sent[-1]) == ["▶️ review → xrev", "🧭 Pick a step", "⏹ Stop the workflow"]


async def test_workflow_offers_what_an_agent_said_since_the_stop(tmp_path: Path, wired) -> None:
    """alpha-engine#454: the run stopped, the work went on outside it, and the
    agent's forward an hour later moved nothing."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **ONE_REVIEW)
    await stopped_on_the_round_limit(channel)
    sent = len(runner.sent)
    await answered(channel, "xrev", "Fixed what nav found.\nDECISION: forward")
    assert len(runner.sent) == sent, "a stopped run is moved by its card, not by a reply"

    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")

    card = api.sent[-1]
    assert "Since it stopped, <b>xrev</b> said <b>forward</b>" in card["text"]
    assert keys_of(card)[0] == "💬 xrev forward: to_nav → nav"
    assert keys_of(card)[-1] == "⏹ Stop the workflow"

    await channel._handle_callback(pressed_transition("flowto", "level3>1>f>xrev"))
    await settled(channel)

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "Fixed what nav found." in text, "it carries the reply of the agent that said it"


async def test_a_step_says_on_its_first_line_that_a_halyard_workflow_sent_it(
    tmp_path: Path, wired
) -> None:
    """Something a project's prompt can point at: keep going, stop nothing."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    await started(channel)

    first_line = runner.sent[0][1].splitlines()[0]
    assert re.fullmatch(
        r"To xrev \(reviewer\), from Halyard workflow level3 \(run [0-9a-f]{8}\), "
        r"step 1 of 2: review — transition: review\.",
        first_line,
    ), first_line


async def test_done_at_the_last_step_finishes_the_run(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Accepted.\nDECISION: done")

    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)
    assert len(runner.sent) == 2


async def test_forward_at_the_last_step_stops_and_the_card_can_end_it(
    tmp_path: Path, wired
) -> None:
    """Ending the workflow is a claim the work is finished; a habitual forward
    leaves it to the operator."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Looks right.\nDECISION: forward")

    assert not any("is done" in sent["text"] for sent in api.sent)
    stopped = api.sent[-1]
    assert "to_nav ended on forward, and only done ends the workflow" in stopped["text"]
    keys = [key["text"] for row in stopped["reply_markup"]["inline_keyboard"] for key in row]
    assert keys == ["⏭ On to the end", "🧭 Pick a step", "⏹ Stop the workflow"]

    await channel._handle_callback(pressed_transition("flowon", "level3"))
    await settled(channel)

    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)


async def test_the_agent_s_own_done_after_that_stop_ends_the_workflow(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Looks right.\nDECISION: forward")

    await answered(channel, "nav", "Checked it again with the operator.\nDECISION: done")

    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)
    assert len(runner.sent) == 2


async def test_a_last_step_that_decided_nothing_stops_rather_than_ending(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "All in.")

    assert not any("is done" in sent["text"] for sent in api.sent)
    assert not any("no decision line" in sent["text"] for sent in api.sent)
    assert "to_nav ended with no decision, and only done ends" in api.sent[-1]["text"]


async def test_done_in_the_middle_of_a_workflow_is_a_sentence(tmp_path: Path, wired) -> None:
    """ "Done." ends many replies; before the last step it decides nothing."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await answered(channel, "xrev", "Read all of it.\nDone.")

    assert [session for session, _ in runner.sent] == ["id-rev"]
    assert "review ended with no decision" in api.sent[-1]["text"]


async def test_a_finished_run_reports_what_it_did_and_is_kept(tmp_path: Path, wired) -> None:
    """When it started and finished, how long, and the steps it took with their
    rounds — in the chat, and in the database beside the tokens."""
    import sqlite3

    channel, api, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    both_twice = {
        "review": {"transition": "review", "seat": "xrev", "rounds": 2},
        "to_nav": {"transition": "to_nav", "seat": "nav", "rounds": 2},
    }
    flow_in(channel, repo, tmp_path, runner, flows=TWO_STEPS["flows"], steps=both_twice)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Look again.\nDECISION: back")
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Accepted.\nDECISION: done")

    [report] = [sent["text"] for sent in api.sent if "is done" in sent["text"]]
    heading, when, steps = report.split("\n")
    # With the run's own name, to find it by in the logs and `halyard runs`.
    assert re.fullmatch(
        r"📋 <b>level3</b> is done — alpha-engine#281 · run <code>[0-9a-f]{8}</code>", heading
    ), heading
    assert " → " in when and "min" in when
    assert steps == "review x2 · to_nav x2"
    with sqlite3.connect(channel._database) as db:
        assert db.execute("SELECT workflow, outcome, deliveries FROM workflow_runs").fetchall() == [
            ("level3", "done", 4)
        ]
        assert db.execute(
            "SELECT step, round, decision, decided_by FROM workflow_steps ORDER BY at"
        ).fetchall() == [
            ("review", 1, "forward", "review"),
            ("to_nav", 1, "back", "to_nav"),
            ("review", 2, "forward", "review"),
            ("to_nav", 2, "done", "to_nav"),
        ]


async def test_a_stopped_run_is_kept_without_a_report(tmp_path: Path, wired) -> None:
    import sqlite3

    channel, api, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await channel._handle_callback(pressed_transition("flowstop", "level3"))
    await settled(channel)

    assert not any("is done" in sent["text"] for sent in api.sent)
    with sqlite3.connect(channel._database) as db:
        assert db.execute("SELECT outcome, deliveries FROM workflow_runs").fetchall() == [
            ("stopped", 1)
        ]


def capped() -> dict:
    """The same flow, with a reviewer that may go once."""
    return {
        "flows": TWO_STEPS["flows"],
        "steps": {
            "review": {"transition": "review", "seat": "xrev", "rounds": 1},
            "to_nav": {"transition": "to_nav", "seat": "nav"},
        },
    }


async def test_a_step_past_its_rounds_is_offered_rather_than_taken(tmp_path: Path, wired) -> None:
    """The lesson of alpha-engine#361, as a button: the round after the last
    one is the operator's to send."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "DECISION: back")

    assert len(runner.sent) == 2, "the round past the cap did not go"
    stopped = api.sent[-1]
    assert "round 2 of 1" in stopped["text"]
    keys = [key["text"] for key in stopped["reply_markup"]["inline_keyboard"][0]]
    assert any(key.startswith("▶️") for key in keys)


async def test_the_operator_can_send_the_step_it_stopped_before(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "DECISION: back")

    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await settled(channel)

    assert runner.sent[-1][0] == "id-rev"
    assert "- Round: 2/1" in runner.sent[-1][1]


async def test_stopping_a_run_forgets_it(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await channel._handle_callback(pressed_transition("flowstop", "level3"))
    await settled(channel)
    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")

    assert any("stopped at step" in sent["text"] for sent in api.sent)
    rows = api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [key["text"] for key in rows[0]] == ["level3"], "it offers the list again"


async def test_a_run_that_is_going_says_where_it_is_rather_than_starting_again(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await started(channel)

    assert len(runner.sent) == 1
    assert "waiting for <b>xrev</b>" in api.sent[-1]["text"]


#: Level 3's review: the reviewer's decision is the navigator's to act on.
REVIEWED = {
    "flows": {"level3": ["review", "reviewed"]},
    "steps": {
        "review": {"transition": "review", "seat": "xrev", "rounds": 2},
        "reviewed": {"transition": "to_nav", "seat": "nav", "rounds": 2, "decided_by": "review"},
    },
}


async def test_a_review_s_back_reaches_the_navigator_and_its_reply_goes_back(
    tmp_path: Path, wired
) -> None:
    """The navigator holds the context: the review goes to it first, and its
    reply — with no decision line of its own — goes where the review said."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **REVIEWED)
    await started(channel)

    await answered(channel, "xrev", "The loader skips a row.\nDECISION: back")

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "- Already decided by review (xrev): back (→ review, xrev, round 2 of 2)" in text

    await answered(channel, "nav", "Fixed the loader; the row is read now.")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "Sent back by: nav (navigator)" in text
    assert "- Round: 2/2" in text
    assert any("<b>review</b>'s <b>back</b> stands" in sent["text"] for sent in api.sent)


async def test_a_decision_the_navigator_acted_on_is_kept_as_the_review_s(
    tmp_path: Path, wired
) -> None:
    """Read back later, a run says on whose word it moved: the navigator's
    replies here carry no decision, and the reviewer's stand. The last one is
    not an end — only done is — so the operator ends it from the card."""
    import sqlite3

    channel, _, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    flow_in(channel, repo, tmp_path, runner, **REVIEWED)
    await started(channel)
    await answered(channel, "xrev", "The loader skips a row.\nDECISION: back")
    await answered(channel, "nav", "Fixed the loader; the row is read now.")
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "The loader reads every row.")
    await channel._handle_callback(pressed_transition("flowon", "level3"))
    await settled(channel)

    with sqlite3.connect(channel._database) as db:
        assert db.execute(
            "SELECT step, round, decision, decided_by FROM workflow_steps ORDER BY at"
        ).fetchall() == [
            ("review", 1, "back", "review"),
            ("reviewed", 1, "back", "review"),
            ("review", 2, "forward", "review"),
            ("reviewed", 2, "forward", "review"),
        ]


async def test_the_navigator_s_own_decision_overrules_the_review(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **REVIEWED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: back")

    await answered(channel, "nav", "That row is out of scope.\nDECISION: done")

    assert len(runner.sent) == 2
    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)


async def test_a_review_that_says_wait_stops_before_the_navigator(tmp_path: Path, wired) -> None:
    """A wait is for the operator. Sent on anyway, the navigator decides for
    itself: the wait was the review's decision, and it is spent."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **REVIEWED)
    await started(channel)

    await answered(channel, "xrev", "The scope needs a decision.\nDECISION: wait")

    assert len(runner.sent) == 1
    keys = [key["text"] for key in api.sent[-1]["reply_markup"]["inline_keyboard"][0]]
    assert any(key.startswith("▶️") for key in keys)

    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await settled(channel)

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "The scope needs a decision." in text, "it carries the reviewer's reply"
    assert "Already decided" not in text
    assert "- Decide on your last line: done" in text, "the navigator's is the last step"


async def test_the_agent_that_asked_to_wait_takes_the_run_on_when_it_decides(
    tmp_path: Path, wired
) -> None:
    """What an agent waits for is usually the operator's answer, given in its
    chat. The replies in between and another wait leave the run where it
    stopped; the decision after them takes it on, carrying that reply."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "Which of the two files is in scope?\nDECISION: wait")

    await answered(channel, "xrev", "Then the second one needs a look as well.")
    await answered(channel, "xrev", "One more thing to settle first.\nDECISION: wait")

    assert len(runner.sent) == 1, "nothing went while it was being talked over"
    assert not any("no decision line" in sent["text"] for sent in api.sent)

    await answered(channel, "xrev", "Both files, then.\nDECISION: forward")

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "Both files, then." in text


async def test_a_round_that_waited_is_kept_as_a_wait(tmp_path: Path, wired) -> None:
    """Read back later, the run says it waited there: the decision after the
    wait answered the operator, not the round."""
    import sqlite3

    channel, _, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "Which file is in scope?\nDECISION: wait")
    await answered(channel, "xrev", "The first one.\nDECISION: forward")

    await answered(channel, "nav", "DECISION: done")

    with sqlite3.connect(channel._database) as db:
        assert db.execute(
            "SELECT step, round, decision, decided_by FROM workflow_steps ORDER BY at"
        ).fetchall() == [("review", 1, "wait", "review"), ("to_nav", 1, "done", "to_nav")]


async def test_a_wait_the_operator_sent_on_is_not_taken_on_again(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "Which file is in scope?\nDECISION: wait")
    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await settled(channel)
    assert runner.sent[-1][0] == "id-nav"

    await answered(channel, "xrev", "The first one.\nDECISION: forward")

    assert len(runner.sent) == 2, "the navigator has it once"


async def test_back_after_a_wait_sends_the_work_to_the_step_before(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Is the second file in scope?\nDECISION: wait")

    await answered(channel, "nav", "It is, and the review missed it.\nDECISION: back")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "Sent back by: nav (navigator)" in text
    assert "- Round: 2/2" in text


async def test_a_step_sent_back_carries_its_seat_s_answer_to_the_round_before(
    tmp_path: Path, wired
) -> None:
    """The lesson of alpha-engine#361: a reviewer asked again is shown what it
    said, rather than finding something new every time."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "BLOCKER: the loader skips a row.\nDECISION: forward")

    await answered(channel, "nav", "Look at it again.\nDECISION: back")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 2/2" in text
    assert "answer to round 1:" in text
    assert "BLOCKER: the loader skips a row." in text


async def test_a_step_that_reached_nobody_counts_no_round(tmp_path: Path, wired) -> None:
    """Sending it again is the same round, not the next — otherwise a followup
    goes to a seat that never saw the first."""
    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    runner.accepting = False

    await started(channel)

    work = await channel._work_item(channel._repositories["alpha-engine"])
    run = flowing.current(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)
    assert run is not None
    assert run.taken() == {}


async def test_a_stopped_run_is_steered_from_a_picked_step_with_its_rounds_kept(
    tmp_path: Path, wired
) -> None:
    """Picking the step keeps the work in the run: a loop it stopped in is not
    reset by a tap."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "DECISION: back")
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "🧭 Pick a step" in keys

    await channel._handle_callback(pressed_transition("flowpick", "level3"))
    await settled(channel)
    offered = api.sent[-1]["reply_markup"]["inline_keyboard"][0]
    assert [key["text"] for key in offered] == ["1 · review", "2 · to_nav"]
    await channel._handle_callback(pressed_transition("flowat", "level3>0"))
    await settled(channel)

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 2/1" in text, "the round it was stopped before, not a fresh first"


#: The same two seats as one phase: the reviewer, then the navigator deciding
#: whether another part of the plan comes next.
PHASED = {
    "flows": {"level3": ["review", "to_nav"]},
    "steps": {
        "review": {"transition": "review", "seat": "xrev", "rounds": 2},
        "to_nav": {"transition": "to_nav", "seat": "nav"},
    },
    "stretches": {"level3": (0, 1)},
}


async def test_next_starts_the_next_phase_and_says_so(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Part one is in.\nDECISION: next")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Phase: 2" in text
    assert "- Round: 1/2" in text, "the second phase's review is its first round"
    assert "Part one is in." in text
    assert any("phase 2 starts at <b>review</b>" in sent["text"] for sent in api.sent)


async def test_next_naming_a_step_further_on_is_marked_in_the_chat(tmp_path: Path, wired) -> None:
    """Leaving the usual path is something whoever reads the chat should see."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Only a proposal this time.\nDECISION: next to_nav")

    assert runner.sent[-1][0] == "id-nav"
    assert "- Phase: 2" in runner.sent[-1][1]
    marked = [sent["text"] for sent in api.sent if sent["text"].startswith("↪️")]
    assert marked and "skipping review" in marked[0]


async def test_a_phase_that_ends_undecided_offers_the_next_one_or_the_way_out(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Part one is in.")

    assert len(runner.sent) == 2, "nothing went while nobody decided"
    stopped = api.sent[-1]
    assert "phase 1 ended with no decision" in stopped["text"]
    keys = [key["text"] for row in stopped["reply_markup"]["inline_keyboard"] for key in row]
    assert keys == [
        "↻ Phase 2 at review",
        "⏭ On to the end",
        "🧭 Pick a step",
        "⏹ Stop the workflow",
    ]

    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await settled(channel)

    assert runner.sent[-1][0] == "id-rev"
    assert "- Phase: 2" in runner.sent[-1][1]


async def test_a_wait_at_the_end_of_a_phase_offers_the_next_one_or_the_way_out(
    tmp_path: Path, wired
) -> None:
    """The operator applies the part just made, then picks what follows."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Accepted; publish it first.\nDECISION: wait")

    assert len(runner.sent) == 2
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert keys == [
        "↻ Phase 2 at review",
        "⏭ On to the end",
        "🧭 Pick a step",
        "⏹ Stop the workflow",
    ]


async def test_after_a_wait_where_a_phase_ended_the_agent_s_next_starts_the_next_phase(
    tmp_path: Path, wired
) -> None:
    """Decided in the phase it waited in: the next one is phase 2, not 3."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Accepted; publish it first.\nDECISION: wait")

    await answered(channel, "nav", "Published. On to part two.\nDECISION: next")

    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Phase: 2" in text
    assert "- Round: 1/2" in text
    assert "Published. On to part two." in text


async def test_after_a_wait_where_the_phases_end_the_workflow_the_agent_s_done_ends_it(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Accepted; publish it first.\nDECISION: wait")

    await answered(channel, "nav", "Published, and that was the last part.\nDECISION: done")

    assert len(runner.sent) == 2
    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)


async def test_where_the_phases_end_the_workflow_forward_offers_both_ways_on(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "Part one is in.\nDECISION: forward")

    assert len(runner.sent) == 2
    stopped = api.sent[-1]
    assert "to_nav ended on forward, and only done ends the workflow" in stopped["text"]
    keys = [key["text"] for row in stopped["reply_markup"]["inline_keyboard"] for key in row]
    assert keys == [
        "↻ Phase 2 at review",
        "⏭ On to the end",
        "🧭 Pick a step",
        "⏹ Stop the workflow",
    ]


async def test_leaving_the_phases_from_the_card_goes_on_past_them(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Part one is in.")

    await channel._handle_callback(pressed_transition("flowon", "level3"))
    await settled(channel)

    assert len(runner.sent) == 2
    assert any("<b>level3</b> is done" in sent["text"] for sent in api.sent)


async def test_leaving_from_an_old_card_moves_nothing_once_the_run_went_on(
    tmp_path: Path, wired
) -> None:
    """Telegram keeps every button pressable. The way out of a phase belongs to
    the stop that offered it, not to whatever the run is doing by then."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    await answered(channel, "nav", "Part one is in.")
    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await settled(channel)
    await answered(channel, "xrev", "The scope needs a decision.\nDECISION: wait")
    sent = len(runner.sent)

    await channel._handle_callback(pressed_transition("flowon", "level3"))
    await settled(channel)

    assert len(runner.sent) == sent
    assert not any("is done" in message["text"] for message in api.sent)
    assert "to_nav · phase 2 — it was asked to wait" in api.sent[-1]["text"]


# --- a run's cards: one live, the newest; every one closed when it ends ------


def run_cards(channel: TelegramChannel) -> list:
    """The run cards still open, oldest first."""
    return [card for card in channel._desk.live() if card.kind == "run"]


def pressed_on(card, what: str, value: str) -> dict:
    """A run's button, pressed on a card the desk sent."""
    from halyard.channels.telegram import cards

    return {
        "id": "cb1",
        "from": {"id": int(APPROVER)},
        "data": cards.choice_data(what, value),
        "message": {"message_id": card.message_id, "chat": {"id": card.chat}},
    }


def closed_with(api, card) -> list[str]:
    return [edit["text"] for edit in api.edits if edit["message_id"] == card.message_id]


async def test_a_run_keeps_one_card_with_buttons_the_newest(tmp_path: Path, wired) -> None:
    """Every step said itself with a Stop under it, and every one stayed: a
    long run left a trail of buttons that all acted on the run going now."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    [first] = run_cards(channel)

    await answered(channel, "xrev", "DECISION: forward")

    [now] = run_cards(channel)
    assert now.message_id != first.message_id
    assert "This run has a newer card." in closed_with(api, first)[-1]


async def test_a_stop_card_takes_the_place_of_the_step_line(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())

    await stopped_on_the_round_limit(channel)

    [card] = run_cards(channel)
    assert "round 2 of 1" in card.html


async def test_asking_where_a_run_is_moves_its_buttons_to_the_answer(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    [line] = run_cards(channel)

    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")

    [where] = run_cards(channel)
    assert where.message_id != line.message_id
    assert "waiting for <b>xrev</b>" in where.html


async def test_a_run_that_is_done_closes_every_card_it_had(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")
    [last] = run_cards(channel)

    await answered(channel, "nav", "All good.\nDECISION: done")

    assert run_cards(channel) == []
    assert "The run is done." in closed_with(api, last)[-1]


async def test_stopping_a_run_from_its_card_closes_every_card_it_had(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    [card] = run_cards(channel)

    await channel._handle_callback(pressed_on(card, "flowstop", "level3"))
    await settled(channel)

    assert run_cards(channel) == []
    assert "The run was stopped." in closed_with(api, card)[-1]


async def test_a_card_of_a_run_left_on_another_branch_steers_nothing_here(
    tmp_path: Path, wired
) -> None:
    """`/command next-task` moves the checkout on, and the last task's run is
    left where it was — its card still in the chat. Its Stop named only the
    workflow, so it stopped the run of the task checked out now."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    [old] = run_cards(channel)
    git(repo, "switch", "-q", "-c", "282-the-next-task")
    await started(channel)

    await channel._handle_callback(pressed_on(old, "flowstop", "level3"))
    await settled(channel)

    assert "From an earlier run" in closed_with(api, old)[-1]
    assert not any("stopped at step" in message["text"] for message in api.sent)
    [going] = run_cards(channel)
    assert going.message_id != old.message_id


async def test_a_run_s_picker_goes_when_the_run_moves(tmp_path: Path, wired) -> None:
    """Offered by the run's card, it leaves that card where it is — and both
    go once the run moves on."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())
    await stopped_on_the_round_limit(channel)
    [stop] = run_cards(channel)

    await channel._handle_callback(pressed_on(stop, "flowpick", "level3"))
    await settled(channel)
    assert len(run_cards(channel)) == 2, "the picker leaves the run's card where it is"
    [picker] = [card for card in run_cards(channel) if card.message_id != stop.message_id]

    await channel._handle_callback(pressed_on(picker, "flowat", "level3>0"))
    await settled(channel)

    [now] = run_cards(channel)
    assert now.message_id not in (stop.message_id, picker.message_id)
    assert "This run has a newer card." in closed_with(api, stop)[-1]


async def test_a_stopped_run_another_workflow_replaces_closes_its_cards(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(
        channel,
        repo,
        tmp_path,
        runner,
        flows={"level3": ["review", "to_nav"], "quick": ["to_nav"]},
        steps=capped()["steps"],
    )
    await stopped_on_the_round_limit(channel)
    [stop] = run_cards(channel)

    await channel._run_workflow("quick", CHAT, None, f"tg:{APPROVER}", start=0)
    await settled(channel)

    assert "Replaced by a run of quick." in closed_with(api, stop)[-1]
    [now] = run_cards(channel)
    assert now.message_id != stop.message_id


async def test_the_phases_stop_past_their_count_and_can_be_sent_anyway(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED, phases=1)
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await answered(channel, "nav", "DECISION: next")

    assert len(runner.sent) == 2
    keys = [key["text"] for key in api.sent[-1]["reply_markup"]["inline_keyboard"][0]]
    assert keys == ["↻ Phase 2 at review anyway", "⏭ On to the end"]


async def test_a_step_of_the_phases_can_be_started_in_the_phase_it_names(
    tmp_path: Path, wired
) -> None:
    """Work that went through its first phase by hand joins the run where it is."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **PHASED)

    await channel._run_workflow("level3 to_nav phase 2", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    session, text = runner.sent[-1]
    assert session == "id-nav"
    assert "- Phase: 2" in text


async def test_a_step_outside_the_phases_has_no_phase_to_start_in(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **{**PHASED, "stretches": {"level3": (1, 1)}})

    await channel._run_workflow("level3 review phase 2", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert runner.sent == []
    assert "no phase to start in" in api.sent[-1]["text"]


# --- a step on its way: taken, and not in its seat's session yet ------------------


async def run_of(channel: TelegramChannel):
    """The run the checkout's work has, as it is on disk."""
    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    work = await channel._work_item(channel._repositories["alpha-engine"])
    return flowing.current(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)


async def heard(channel: TelegramChannel, seat: str, text: str) -> None:
    """A seat's reply arriving while a step is still on its way: what the reply
    moves is waited for, and the step is left where it is."""
    session_id, name = SESSIONS[seat]
    await channel.send_message(
        session_id, text, agent_id="claude-code", session_name=name, project="alpha-engine"
    )
    moving = [task for task in channel._sending if task.get_name() == "/workflow"]
    await asyncio.wait_for(asyncio.gather(*moving), timeout=5)


def inspected(channel: TelegramChannel) -> None:
    """The review step's transition with an inspection to run first."""
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], inspections=("proof",))
    channel._repositories["alpha-engine"] = replace(
        found, transitions={**found.transitions, "review": review}
    )


class Held:
    """An inspection that answers when it is told to — the minutes a step's
    inspections take, held open so a test can act inside them."""

    def __init__(self, runner: FakeRunner) -> None:
        self.asked = asyncio.Event()
        self.answer = asyncio.Event()
        runner.ask = self.ask

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        self.asked.set()
        await self.answer.wait()
        return "proof · no finding"


async def test_a_reply_while_a_step_is_being_prepared_is_not_its_answer(
    tmp_path: Path, wired, caplog
) -> None:
    """The run used to wait on the seat from the moment the step was taken, so
    a reply that came while its inspections ran was taken as the step's answer
    and moved the run on before the step had gone anywhere."""
    caplog.set_level(logging.INFO, logger="halyard.channels.telegram.adapter")
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    held = Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    await asyncio.wait_for(held.asked.wait(), timeout=5)

    await heard(channel, "xrev", "The earlier work is done.\nDECISION: forward")
    run = await run_of(channel)

    assert runner.sent == [], "nothing went anywhere before the step did"
    assert run is not None
    assert (run.step, run.sending_to, run.waiting) == (0, "xrev", False)
    assert "xrev replied before step 1 reached it; not its answer" in caplog.text

    held.answer.set()
    await going
    await settled(channel)
    assert [session for session, _ in runner.sent] == ["id-rev"]
    run = await run_of(channel)
    assert run is not None and (run.waiting, run.waiting_for) == (True, "xrev")

    await answered(channel, "xrev", "DECISION: forward")
    assert runner.sent[-1][0] == "id-nav"


async def test_a_step_on_its_way_says_so_offers_a_step_instead_and_is_not_sent_twice(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    held = Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    await asyncio.wait_for(held.asked.wait(), timeout=5)

    await channel._run_workflow("", CHAT, None, f"tg:{APPROVER}")
    asked_where = api.sent[-1]
    await channel._handle_callback(pressed_transition("flowgo", "level3"))
    await asyncio.sleep(0.05)
    pressed = api.sent[-1]["text"]
    held.answer.set()
    await going
    await settled(channel)

    assert "review — on its way to <b>xrev</b>." in asked_where["text"]
    keys = [key["text"] for row in asked_where["reply_markup"]["inline_keyboard"] for key in row]
    assert keys == ["🧭 Pick a step", "⏹ Stop the workflow"]
    assert "on its way to <b>xrev</b>" in pressed
    assert [session for session, _ in runner.sent] == ["id-rev"]


async def test_a_step_behind_a_turn_its_seat_is_in_is_not_answered_by_that_turn(
    tmp_path: Path, wired
) -> None:
    """A message to a busy session waits behind the turn going there, and that
    turn's reply arrives first: it answers what started it, not the step."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    free = asyncio.Event()

    async def behind(session_id: str, began) -> bool:
        await free.wait()
        await began(session_id)
        return True

    runner.turn = behind
    await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    for _ in range(200):
        if runner.sent:
            break
        await asyncio.sleep(0.01)

    await heard(channel, "xrev", "What the turn ahead of it was for.\nDECISION: forward")
    run = await run_of(channel)

    assert [session for session, _ in runner.sent] == ["id-rev"]
    assert run is not None and (run.sending_to, run.waiting) == ("xrev", False)

    free.set()
    await settled(channel)
    run = await run_of(channel)
    assert run is not None and (run.waiting, run.waiting_for) == (True, "xrev")

    await answered(channel, "xrev", "DECISION: forward")
    assert runner.sent[-1][0] == "id-nav"


async def test_a_turn_that_answers_before_it_counts_as_accepted_still_moves_the_run(
    tmp_path: Path, wired
) -> None:
    """A turn over inside the acceptance window has relayed its reply before it
    is called accepted: waiting from acceptance would ignore that reply, and
    the run would wait on a seat that had already answered."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    async def quick(session_id: str, began) -> bool:
        runner.turn = None
        await began(session_id)
        session, name = SESSIONS["xrev"]
        await channel.send_message(
            session,
            "Nothing to add.\nDECISION: forward",
            agent_id="claude-code",
            session_name=name,
            project="alpha-engine",
        )
        return True

    runner.turn = quick
    await started(channel)

    assert [session for session, _ in runner.sent] == ["id-rev", "id-nav"]


async def test_a_step_its_runtime_took_without_saying_so_first_is_waited_on(
    tmp_path: Path, wired
) -> None:
    """Taken is there, whether or not the runtime said when its turn started."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    async def silent(_session_id: str, _began) -> bool:
        return True

    runner.turn = silent
    await started(channel)
    run = await run_of(channel)

    assert run is not None
    assert (run.waiting, run.waiting_for, run.sending_to) == (True, "xrev", "")
    assert run.taken() == {"review": 1}


async def test_a_step_that_reached_nobody_stops_the_run_ready_to_send_again(
    tmp_path: Path, wired
) -> None:
    """It used to leave the run waiting, under the chat's warning, for a reply
    nothing had been asked for."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    runner.accepting = False

    await started(channel)
    run = await run_of(channel)

    assert run is not None
    assert run.stopped == "review did not reach xrev: nothing was printed"
    assert (run.waiting, run.sending_to) == (False, "")
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "▶️ review → xrev" in keys

    runner.accepting = True
    await tap(channel, pressed_transition("flowgo", "level3"))
    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 1/2" in text, "the same round: the first reached nobody"


async def test_pausing_while_a_step_is_prepared_stops_it_where_it_would_have_gone(
    tmp_path: Path, wired
) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    held = Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    await asyncio.wait_for(held.asked.wait(), timeout=5)

    await channel._gate.pause(f"tg:{APPROVER}")
    held.answer.set()
    await going
    await settled(channel)
    run = await run_of(channel)

    assert runner.sent == []
    assert run is not None and run.stopped == "review did not reach xrev: Halyard is paused"


async def test_a_step_a_command_still_running_holds_back_stops_the_run(
    tmp_path: Path, wired
) -> None:
    """One command at a time per project: the step did not go, so its run is
    stopped rather than left waiting on it."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], commands=("test-fast",))
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={"test-fast": "make test-fast"},
        transitions={**found.transitions, "review": review},
    )
    channel._working["alpha-engine"] = "transition close"

    await started(channel)
    run = await run_of(channel)

    assert runner.sent == []
    assert run is not None
    assert run.stopped == "review: transition close is still running, one at a time"


async def test_a_step_that_broke_before_it_was_sent_stops_the_run(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    async def broken(_found):
        raise RuntimeError("the tracker answered nonsense")

    channel._task_labels = broken

    with pytest.raises(RuntimeError):
        await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    run = await run_of(channel)

    assert runner.sent == []
    assert run is not None
    assert run.stopped == (
        "review did not reach xrev: it broke before it was sent — the log says why"
    )


def left_open(tmp_path: Path, channel: TelegramChannel, *cards_left) -> None:
    """What the last process left open, written down as it would have been."""
    from halyard.channels.telegram.shelf import Shelf

    shelf = Shelf(tmp_path / "halyard.db")
    for kept in cards_left:
        shelf.put(kept)
    channel._desk._shelf = shelf
    channel._desk._pace = 0


async def test_a_step_a_restart_caught_on_its_way_stops_ready_to_send_again(
    tmp_path: Path, wired
) -> None:
    """Its commands, its inspections and the message went with the process, so
    nothing reached the seat — and the run used to wait on it for good."""
    from datetime import UTC, datetime

    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE
    from halyard.channels.telegram.shelf import Kept

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    work = await channel._work_item(channel._repositories["alpha-engine"])
    since = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    sent = flowing.Run(
        workflow="level3", step=0, since=since, chat=CHAT, waiting=False, ident="3f2a9c1e"
    ).sending("xrev")
    flowing.save(channel._kept_for("alpha-engine", WORKFLOW_FILE), work, sent)
    left_open(tmp_path, channel, Kept(CHAT, 77, "run", "3f2a9c1e", "🤝 review → xrev…", since))

    await channel.start()
    await settled(channel)
    await channel.stop()
    run = await run_of(channel)

    assert run is not None
    assert run.stopped == "review was interrupted before it reached xrev: Halyard restarted"
    [closed] = api.edits
    assert closed["message_id"] == 77
    assert closed["text"] == "🤝 review → xrev…\n\n↪️ <i>This run has a newer card.</i>"
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "▶️ review → xrev" in keys

    await tap(channel, pressed_transition("flowgo", "level3"))
    assert [session for session, _ in runner.sent] == ["id-rev"]


async def test_a_run_waiting_when_halyard_restarted_waits_on_and_its_card_says_so(
    tmp_path: Path, wired
) -> None:
    """Written as the last release wrote it, before steps were on their way:
    still waiting, and its card closes saying the run is kept, not that it ended."""
    import json
    from datetime import UTC, datetime

    from halyard.channels.telegram.adapter import WORKFLOW_FILE
    from halyard.channels.telegram.shelf import Kept

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    work = await channel._work_item(channel._repositories["alpha-engine"])
    kept = channel._kept_for("alpha-engine", WORKFLOW_FILE)
    assert kept is not None and work is not None
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(
        json.dumps(
            {
                work: {
                    "workflow": "level3",
                    "step": 0,
                    "since": "2026-10-25T09:00:00+00:00",
                    "chat": CHAT,
                    "waiting_for": "xrev",
                    "waiting": True,
                    "ident": "3f2a9c1e",
                }
            }
        )
    )
    since = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    left_open(tmp_path, channel, Kept(CHAT, 77, "run", "3f2a9c1e", "🤝 review → xrev…", since))

    await channel.start()
    await settled(channel)
    await channel.stop()
    run = await run_of(channel)

    [closed] = api.edits
    assert closed["text"] == (
        "🤝 review → xrev…\n\n"
        "⏹ <i>Closed — Halyard restarted. The run is kept: /workflow shows it.</i>"
    )
    assert run is not None and (run.waiting, run.waiting_for, run.stopped) == (True, "xrev", "")

    await answered(channel, "xrev", "DECISION: forward")
    assert runner.sent[-1][0] == "id-nav"


# --- a step's turn ending without an answer ----------------------------------------


async def test_a_step_whose_turn_ends_without_an_answer_stops_the_run_ready_to_send_again(
    tmp_path: Path, wired
) -> None:
    """Out of usage halfway, most often. The run used to wait for a reply that
    nothing was writing."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)

    await runner.when_done("Claude AI usage limit reached · resets 3pm")
    await settled(channel)
    run = await run_of(channel)

    assert run is not None
    assert run.stopped == (
        "review's turn in xrev ended without an answer: Claude AI usage limit reached · resets 3pm"
    )
    assert run.taken() == {}, "the round its turn counted is given back"
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "▶️ review → xrev" in keys

    await tap(channel, pressed_transition("flowgo", "level3"))
    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 1/2" in text, "the same round: the first was never answered"


async def test_a_turn_that_fails_after_its_answer_came_moves_nothing(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await started(channel)
    review_s_turn = runner.when_done
    await answered(channel, "xrev", "DECISION: forward")

    await review_s_turn("it stopped")
    await settled(channel)
    run = await run_of(channel)

    assert run is not None
    assert (run.waiting, run.waiting_for, run.stopped) == (True, "nav", "")
    assert run.taken() == {"review": 1, "to_nav": 1}


def typed_in(chat: str, text: str, *, user: str = APPROVER) -> dict:
    """Something typed in another chat than the default one — an agent's own."""
    return {"message_id": 1, "from": {"id": int(user)}, "chat": {"id": chat}, "text": text}


#: The reviewer's own chat, where `/stop` reaches its session.
XREV_CHAT = "-100888"


async def test_stop_on_a_step_s_turn_before_it_answered_stops_the_run_ready_to_send_again(
    tmp_path: Path, wired
) -> None:
    """The turn was ended on purpose, so nothing reported it: the run waited
    for an answer nothing was writing."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    runner.stop = lambda session_id, cwd=None: True
    await started(channel)

    await deliver(channel, typed_in(XREV_CHAT, "/stop"))
    run = await run_of(channel)

    assert run is not None
    assert run.stopped == f"review's turn in xrev was stopped by tg:{APPROVER} before it answered"
    assert run.taken() == {}, "the round its turn counted is given back"
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "▶️ review → xrev" in keys

    await tap(channel, pressed_transition("flowgo", "level3"))
    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 1/2" in text, "the same round: the first was never answered"


async def test_stop_that_drops_a_step_queued_behind_the_turn_says_who_stopped_it(
    tmp_path: Path, wired
) -> None:
    """The step waited behind a turn its agent was in, and went with it: it was
    stopped, not a message that reached nobody with nothing printed."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    dropped = asyncio.Event()

    async def behind(_session_id: str, _began) -> bool:
        await dropped.wait()
        return False

    def stop(_session_id: str, cwd=None) -> bool:
        # The turn going in the session ends, and what waited behind it goes.
        dropped.set()
        return True

    runner.turn = behind
    runner.stop = stop
    await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    for _ in range(200):
        if runner.sent:
            break
        await asyncio.sleep(0.01)

    await deliver(channel, typed_in(XREV_CHAT, "/stop"))
    run = await run_of(channel)

    assert run is not None
    assert run.stopped == f"review was stopped by tg:{APPROVER} before it reached xrev"
    assert run.taken() == {}
    keys = [key["text"] for row in api.sent[-1]["reply_markup"]["inline_keyboard"] for key in row]
    assert "▶️ review → xrev" in keys
    assert not any("did not reach" in sent["text"] for sent in api.sent)

    runner.turn = None
    await tap(channel, pressed_transition("flowgo", "level3"))
    session, text = runner.sent[-1]
    assert session == "id-rev"
    assert "- Round: 1/2" in text


async def test_stop_after_the_step_was_answered_moves_nothing(tmp_path: Path, wired) -> None:
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    runner.stop = lambda session_id, cwd=None: True
    await started(channel)
    await answered(channel, "xrev", "DECISION: forward")

    await deliver(channel, typed_in(XREV_CHAT, "/stop"))
    run = await run_of(channel)

    assert run is not None
    assert (run.waiting, run.waiting_for, run.stopped) == (True, "nav", "")
    assert run.taken() == {"review": 1, "to_nav": 1}


async def test_stop_on_an_earlier_turn_of_the_step_leaves_the_step_on_its_way(
    tmp_path: Path, wired
) -> None:
    """Answered without a decision, then sent again behind a turn its agent is
    in: what `/stop` ends there is not the step's own turn, which has not
    started — and the run is not waiting on it."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    runner.stop = lambda session_id, cwd=None: True
    await started(channel)
    await answered(channel, "xrev", "I looked at it.")
    free = asyncio.Event()

    async def behind(session_id: str, began) -> bool:
        await free.wait()
        return await began(session_id) is not False

    runner.turn = behind
    await channel._run_workflow("level3 review", CHAT, None, f"tg:{APPROVER}")
    for _ in range(200):
        if len(runner.sent) == 2:
            break
        await asyncio.sleep(0.01)

    await channel._handle_message(typed_in(XREV_CHAT, "/stop"))
    run = await run_of(channel)

    assert run is not None and (run.sending_to, run.stopped) == ("xrev", "")
    free.set()
    await settled(channel)
    run = await run_of(channel)
    assert run is not None and (run.waiting, run.waiting_for) == (True, "xrev")


# --- a round counted as its turn starts ----------------------------------------------


async def say_from(channel: TelegramChannel, seat: str, text: str) -> None:
    """A seat's reply, relayed — whatever it moves left to run."""
    session_id, name = SESSIONS[seat]
    await channel.send_message(
        session_id, text, agent_id="claude-code", session_name=name, project="alpha-engine"
    )


async def test_an_answer_back_in_seconds_marks_the_round_it_answers(tmp_path: Path, wired) -> None:
    """Counted when the session took the step, the round came after a quick
    answer: the decision found no round to mark, and the round came in blank."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)

    async def quick(session_id: str, began) -> bool:
        runner.turn = None
        if began is not None and await began(session_id) is False:
            return False
        await say_from(channel, "xrev", "Nothing to add.\nDECISION: forward")
        # Acted on before the turn is called accepted, as an answer inside
        # the acceptance window is.
        moving = [task for task in channel._sending if task.get_name() == "/workflow"]
        await asyncio.gather(*moving)
        return True

    runner.turn = quick
    await started(channel)
    run = await run_of(channel)

    assert run is not None
    [review] = run.rounds["review"]
    assert (review.to, review.decision, review.decided_by) == ("xrev", "forward", "review")


async def test_the_round_limit_holds_for_answers_back_in_seconds(tmp_path: Path, wired) -> None:
    """A step allowed once, answered in seconds: its round has to be there when
    the next answer is weighed, or the step goes a second time."""
    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **capped())
    accepted = asyncio.Event()

    async def quick(session_id: str, began) -> bool:
        if began is not None and await began(session_id) is False:
            return False
        if session_id == "id-rev":
            await say_from(channel, "xrev", "DECISION: forward")
            # Not called accepted until the run has moved on twice.
            await accepted.wait()
            return True
        await say_from(channel, "nav", "DECISION: back")
        return True

    runner.turn = quick
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    for _ in range(300):
        run = await run_of(channel)
        reviews = [session for session, _ in runner.sent if session == "id-rev"]
        if (run is not None and run.stopped) or len(reviews) > 1:
            break
        await asyncio.sleep(0.01)
    accepted.set()
    await going
    await settled(channel)
    run = await run_of(channel)

    assert [session for session, _ in runner.sent] == ["id-rev", "id-nav"]
    assert run is not None and run.stopped == "review would go for round 2 of 1"
    assert run.taken() == {"review": 1, "to_nav": 1}


# --- a step on its way, called back -------------------------------------------------


async def test_stopping_a_run_calls_back_a_step_whose_inspections_are_running(
    tmp_path: Path, wired
) -> None:
    """The run was cleared, and the step went on: its inspections finished, and
    its message reached an agent whose answer then moved nothing."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    check = CheckAsking(channel, runner, waits=True)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    await check.until_asked()

    await tap(channel, pressed_transition("flowstop", "level3"))
    await asyncio.wait_for(going, timeout=5)
    [asked] = check.asked

    assert runner.sent == [], "the step never reached its agent"
    assert await run_of(channel) is None
    assert channel._inspecting == {}
    assert await channel._store.resolution_of(asked.request_id) is not None, "its card is closed"
    said = [sent["text"] for sent in api.sent]
    assert "⏹ <b>review</b> was stopped before it reached <b>xrev</b>." in said
    assert not any("proof</b>:" in text for text in said), "nothing it found is reported"


async def test_stopping_a_run_ends_the_command_its_step_on_its_way_is_running(
    tmp_path: Path, wired
) -> None:
    """Ended as `/stop` ends one, and waited for: the run's next step cannot
    find it still going."""
    import os
    import time

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    pid_file = tmp_path / "slow.pid"
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], commands=("slow",))
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={"slow": f"echo $$ > {pid_file}; exec sleep 30"},
        transitions={**found.transitions, "review": review},
    )
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())

    begun = time.monotonic()
    try:
        await tap(channel, pressed_transition("flowstop", "level3"))
        await asyncio.wait_for(going, timeout=10)
        elapsed = time.monotonic() - begun

        assert elapsed < 10
        try:
            os.kill(pid, 0)
            still_there = True
        except ProcessLookupError:
            still_there = False
        assert not still_there, "the command was left running"
        assert channel._working == {} and channel._stops == {}
        assert runner.sent == []
        said = [sent["text"] for sent in api.sent]
        assert "⏹ <b>review</b> was stopped before it reached <b>xrev</b>." in said
        assert not any("a command did not run" in text for text in said)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


async def test_steering_off_a_step_on_its_way_calls_it_back_and_takes_the_one_named(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    for _ in range(500):
        if channel._inspecting:
            break
        await asyncio.sleep(0.01)

    await asyncio.wait_for(
        channel._run_workflow("level3 to_nav", CHAT, None, f"tg:{APPROVER}"), timeout=5
    )
    await asyncio.wait_for(going, timeout=5)
    await settled(channel)
    run = await run_of(channel)

    assert [session for session, _ in runner.sent] == ["id-nav"]
    assert channel._inspecting == {}
    assert run is not None and (run.waiting, run.waiting_for) == (True, "nav")
    assert run.taken() == {"to_nav": 1}, "the step called back counted nothing"
    said = [sent["text"] for sent in api.sent]
    assert "⏹ <b>review</b> was stopped before it reached <b>xrev</b>." in said


async def test_a_step_picked_while_another_is_on_its_way_takes_its_place(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    for _ in range(500):
        if channel._inspecting:
            break
        await asyncio.sleep(0.01)

    await channel._handle_callback(pressed_transition("flowpick", "level3"))
    for _ in range(200):
        rows = (api.sent[-1].get("reply_markup") or {}).get("inline_keyboard") or []
        if rows and rows[0][0]["text"] == "1 · review":
            break
        await asyncio.sleep(0.01)
    offered = api.sent[-1]["reply_markup"]["inline_keyboard"][0]
    assert [key["text"] for key in offered] == ["1 · review", "2 · to_nav"]
    await channel._handle_callback(pressed_transition("flowat", "level3>1"))
    await asyncio.wait_for(going, timeout=5)
    await settled(channel)

    assert [session for session, _ in runner.sent] == ["id-nav"]
    assert "⏹ <b>review</b> was stopped before it reached <b>xrev</b>." in [
        sent["text"] for sent in api.sent
    ]


async def test_a_step_its_run_no_longer_sends_is_never_delivered(tmp_path: Path, wired) -> None:
    """Stopped while its message waited behind its agent's turn: asked as its
    own turn would begin, it does not go."""
    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    free = asyncio.Event()
    withdrawn: list[str] = []

    async def behind(session_id: str, began) -> bool:
        await free.wait()
        if await began(session_id) is False:
            withdrawn.append(session_id)
            return False
        return True

    runner.turn = behind
    await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    for _ in range(200):
        if runner.sent:
            break
        await asyncio.sleep(0.01)

    await channel._handle_callback(pressed_transition("flowstop", "level3"))
    for _ in range(200):
        if await run_of(channel) is None:
            break
        await asyncio.sleep(0.01)
    free.set()
    await settled(channel)

    assert withdrawn == ["id-rev"]
    said = [sent["text"] for sent in api.sent]
    assert "⏹ <b>review</b> was stopped before it reached <b>xrev</b>." in said
    assert not any("did not reach" in text for text in said)


async def test_a_step_whose_run_changed_while_it_was_prepared_is_not_handed_on(
    tmp_path: Path, wired
) -> None:
    """Checked right before it goes, whatever changed the run."""
    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspected(channel)
    held = Held(runner)
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    await asyncio.wait_for(held.asked.wait(), timeout=5)
    work = await channel._work_item(channel._repositories["alpha-engine"])
    flowing.clear(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)

    held.answer.set()
    await asyncio.wait_for(going, timeout=5)
    await settled(channel)

    assert runner.sent == []
    said = [sent["text"] for sent in api.sent]
    assert not any("sent to <b>xrev</b>" in text for text in said)
    assert not any("proof</b>: answered" in text for text in said)
    assert await run_of(channel) is None


async def test_a_command_stopped_after_its_run_changed_does_not_bring_the_run_back(
    tmp_path: Path, wired
) -> None:
    """A step stopped where it is holds its run — but only the run that still
    sends it: one cleared meanwhile stays cleared."""
    import os

    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    pid_file = tmp_path / "slow.pid"
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], commands=("slow",))
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={"slow": f"echo $$ > {pid_file}; exec sleep 30"},
        transitions={**found.transitions, "review": review},
    )
    going = asyncio.create_task(
        channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    )
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    work = await channel._work_item(channel._repositories["alpha-engine"])
    try:
        flowing.clear(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)
        channel._stop_command(CHAT, f"tg:{APPROVER}")
        await asyncio.wait_for(going, timeout=10)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)

    assert await run_of(channel) is None


async def test_a_step_stopped_right_after_a_restart_is_not_brought_back(
    tmp_path: Path, wired
) -> None:
    from datetime import UTC, datetime

    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    work = await channel._work_item(channel._repositories["alpha-engine"])
    kept = channel._kept_for("alpha-engine", WORKFLOW_FILE)
    since = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    flowing.save(
        kept,
        work,
        flowing.Run(
            workflow="level3", step=0, since=since, chat=CHAT, waiting=False, ident="3f2a9c1e"
        ).sending("xrev"),
    )
    runs = channel._runs_left()
    flowing.clear(kept, work)

    await channel._after_restart([], runs)

    assert await run_of(channel) is None
    assert api.sent == []


async def test_a_transition_runs_its_checks_before_it_goes(tmp_path: Path, wired) -> None:
    """The reply and what the checks made of it arrive together, and the chat
    it was sent from is told which checks answered."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )

    await channel._run_transition("discovery delivery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert len(runner.asked) == 1
    [(_, text)] = runner.sent
    assert "Inspection proof — NOTES/proof.md" in text
    assert runner.says in text
    assert "Said by whoever asked: delivery" in text
    assert any("<b>proof</b>: complete" in sent["text"] for sent in api.sent)


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


async def test_a_command_a_check_asks_for_reaches_where_the_transition_goes(
    tmp_path: Path, wired
) -> None:
    """The check's turn is nobody's seat. Its command is a card in the chat of
    the seat the transition is for, saying which check and which transition want it
    — and the turn stands in the project, with nothing that edits."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )
    check = CheckAsking(channel, runner)

    await channel._run_transition("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [how] = check.started
    assert how["cwd"] == repo
    assert how["edits"] is False
    [card] = [sent for sent in api.sent if "PERMISSION REQUEST" in sent["text"]]
    assert card["chat_id"] == "-100888"
    assert card["text"].startswith("<b>[INSPECTION — PERMISSION REQUEST]</b>")
    assert "Inspection: <b>proof · transition discovery</b>" in card["text"]
    keys = [key["text"] for row in card["reply_markup"]["inline_keyboard"] for key in row]
    assert keys[-1] == "⏹ Stop the inspection"
    assert channel._inspecting == {}


async def test_a_command_a_check_asks_for_here_is_carded_here(tmp_path: Path, wired) -> None:
    """`/checks` hands nothing on: the card says which check, in the chat that asked."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    CheckAsking(channel, runner)

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    [card] = [sent for sent in api.sent if "PERMISSION REQUEST" in sent["text"]]
    assert card["chat_id"] == CHAT
    assert "Inspection: <b>proof</b>" in card["text"]


async def test_stop_on_a_checks_card_ends_the_check_and_says_who(tmp_path: Path, wired) -> None:
    """Deny refuses one command and the check tries the next; Stop refuses it
    and ends the check. Measured: a claims check told only that it could run
    commands asked for one after another — the suite test by test, probes of
    its own, scratch copies — and nothing short of a restart could end it. The
    card says who stopped it, and the answer is an unmeasured one saying why."""
    from halyard.core.approvals import Decision

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    check = CheckAsking(channel, runner, waits=True)

    await channel._handle_callback(pressed_inspection("proof"))
    await check.until_asked()
    [request] = check.asked
    await channel._handle_callback(stopping(request))
    await settled(channel)

    resolution = await channel._store.resolution_of(request.request_id)
    assert resolution.decision is Decision.DENY
    assert any(edit["text"].startswith(f"<b>⏹ STOPPED</b> by tg:{APPROVER}") for edit in api.edits)
    said = [sent["text"] for sent in api.sent]
    assert (
        f"<b>proof</b> · unmeasured — stopped by tg:{APPROVER}\n\n"
        + (
            "Halyard: 1 read this inspection asked for did not run — `make test-fast` "
            f"(denied by tg:{APPROVER}). Anything that rests on it is not measured."
        )
        in said
    )
    assert channel._inspecting == {}


async def test_a_transition_goes_on_without_a_check_somebody_stopped(tmp_path: Path, wired) -> None:
    """Stop ends that check, not the transition: the seat still gets the reply,
    with the stopped check said to be unmeasured and why."""
    channel, _, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )
    check = CheckAsking(channel, runner, waits=True)

    handing = asyncio.ensure_future(
        channel._run_transition("discovery", CHAT, None, f"tg:{APPROVER}")
    )
    await check.until_asked()
    await channel._handle_callback(stopping(check.asked[0]))
    await handing
    await settled(channel)

    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert f"stopped by tg:{APPROVER}" in text


# --- reads an inspection asked for that never ran -------------------------------------


class ReadsAsked:
    """An inspection's turn that asks for reads, each a card the way the gate
    brings one in — under the id its runtime runs the turn by, when that is
    another — and then answers, whatever became of its cards, the way a Codex
    model can. `meanwhile` is what happens to the cards before it answers."""

    def __init__(
        self,
        channel: TelegramChannel,
        runner: FakeRunner,
        *reads: str,
        under: str = "",
        meanwhile=None,
        ran: tuple[tuple[str, int], ...] = (),
        says: str | None = "claims · no finding",
    ) -> None:
        self.channel = channel
        self.reads = reads
        self.under = under
        self.meanwhile = meanwhile
        #: Commands its runtime reports it ran, with their exit codes — in
        #: the shape `codex exec --json` says them.
        self.ran = ran
        self.says = says
        self.asked: list = []
        runner.ask = self.ask

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        from halyard.agents.codex.runner import ran
        from halyard.core.events import RiskLevel

        ident = kwargs["session_id"]
        if self.under:
            # The runtime says the id it runs the turn by, as Codex does.
            kwargs["started"](self.under)
            ident = self.under
        if self.ran:
            calls = kwargs["calls"]
            calls.reported = True
            for number, (command, code) in enumerate(self.ran):
                item = {
                    "id": f"item_{number}",
                    "type": "command_execution",
                    "command": f"/bin/zsh -lc '{command}'",
                    "aggregated_output": "what it printed",
                    "exit_code": code,
                    "status": "completed",
                }
                ran(calls, "item.started", {**item, "exit_code": None, "status": "in_progress"})
                ran(calls, "item.completed", item)
        for command in self.reads:
            request = await self.channel._store.create(
                session_id=ident,
                agent_id="codex",
                project="alpha-engine",
                tool="Bash",
                command_summary=command,
                command_full=command,
                risk=RiskLevel.HIGH,
            )
            await self.channel.send_approval_request(request)
            self.asked.append(request)
        if self.meanwhile is not None:
            await self.meanwhile(self.asked)
        return self.says


async def decided(channel: TelegramChannel, request, decision: str) -> None:
    """A card answered with its own button, by somebody allowed to."""
    from halyard.channels.telegram import cards

    await channel._handle_callback(
        {
            "id": "cbq-2",
            "from": {"id": int(APPROVER)},
            "data": cards.callback_data(request, decision),
        }
    )


ENDED_X = (
    "Halyard: 1 read this inspection asked for did not run — `cat /tmp/x.log` "
    "(still waiting for approval when the inspection ended). "
    "Anything that rests on it is not measured."
)


async def test_an_inspection_that_answers_with_a_card_open_closes_it_and_says_so(
    tmp_path: Path, wired
) -> None:
    """Measured on Codex: the model went on and answered while its card was
    still open, the read never ran, and the card stayed on the phone until it
    expired — allowing it then did nothing. The card here carries the id the
    runtime ran the turn under, not the one Halyard chose."""
    from halyard.core.approvals import Decision, ResolutionReason

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    asking = ReadsAsked(channel, runner, "cat /tmp/x.log", under="codex-thread-1")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)
    [request] = asking.asked
    resolution = await channel._store.resolution_of(request.request_id)

    assert request.session_id == "codex-thread-1"
    assert resolution is not None
    assert (resolution.decision, resolution.reason) == (Decision.DENY, ResolutionReason.ENDED)
    [closed] = [edit for edit in api.edits if "THE INSPECTION ENDED" in edit["text"]]
    assert "The inspection ended before this was answered, so nothing ran." in closed["text"]
    answered = [sent["text"] for sent in api.sent if sent["text"].startswith("<b>proof</b> · ")]
    assert answered and answered[-1].endswith(f"</pre>\n\n{ENDED_X}")


async def test_a_denied_card_and_an_expired_one_are_said_with_what_happened(
    tmp_path: Path, wired
) -> None:
    """And an allowed one ran, so it is not said at all."""
    from halyard.channels.telegram import cards
    from halyard.core.approvals import ResolutionReason

    async def meanwhile(asked) -> None:
        denied, expired, allowed = asked
        await decided(channel, denied, cards.DENY)
        await channel._store.deny(
            expired.request_id, reason=ResolutionReason.TIMEOUT, note="Nobody answered."
        )
        await decided(channel, allowed, cards.ALLOW)

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(channel, runner, "cat a", "cat b", "cat c", meanwhile=meanwhile)

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    said = "\n".join(sent["text"] for sent in api.sent)
    assert (
        "Halyard: 2 reads this inspection asked for did not run — "
        f"`cat a` (denied by tg:{APPROVER}); `cat b` (expired before anybody answered). "
        "Anything that rests on them is not measured."
    ) in said
    assert "`cat c`" not in said


async def test_an_inspection_whose_reads_were_all_allowed_adds_nothing(
    tmp_path: Path, wired
) -> None:
    from halyard.channels.telegram import cards

    async def meanwhile(asked) -> None:
        for request in asked:
            await decided(channel, request, cards.ALLOW)

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(channel, runner, "cat a", meanwhile=meanwhile)

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    assert not any("Halyard:" in sent["text"] for sent in api.sent)
    assert not any("THE INSPECTION ENDED" in edit["text"] for edit in api.edits)


async def test_reads_that_did_not_run_reach_the_transition_s_message_and_outcome_line(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )
    ReadsAsked(channel, runner, "cat /tmp/x.log", under="codex-thread-2")

    await channel._run_transition("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [(_, text)] = runner.sent
    assert f"claims · no finding\n\n{ENDED_X}" in text
    assert any(
        "<b>proof</b>: incomplete · 1 read seen at the gate, 1 did not run" in sent["text"]
        for sent in api.sent
    )


async def test_reads_that_did_not_run_are_kept_with_the_run(tmp_path: Path, wired) -> None:
    """In a column of their own, beside the model's answer as it came."""
    import sqlite3

    channel, _, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    channel._keep_inspections = True
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(channel, runner, "cat /tmp/x.log")

    await channel._run_inspection("proof", CHAT, None)
    await settled(channel)

    with sqlite3.connect(channel._database) as db:
        [(answer, not_run)] = db.execute("SELECT answer, not_run FROM inspection_runs").fetchall()
    assert (answer, not_run) == ("claims · no finding", ENDED_X)


async def test_stop_closes_a_card_its_runtime_s_own_id_carries(tmp_path: Path, wired) -> None:
    """Stop said so only on the cards under the id Halyard chose; one under
    the id its runtime ran the turn by — a Codex thread — stayed open."""
    from halyard.core.approvals import Decision

    async def meanwhile(asked) -> None:
        [chosen] = [s for s, i in channel._inspecting.items() if i.name == "proof"]
        await channel._stop_inspection(chosen, f"tg:{APPROVER}")
        await asyncio.sleep(1)

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    asking = ReadsAsked(
        channel, runner, "cat /tmp/x.log", under="codex-thread-3", meanwhile=meanwhile
    )

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)
    [request] = asking.asked
    resolution = await channel._store.resolution_of(request.request_id)

    assert resolution is not None and resolution.decision is Decision.DENY
    assert any(edit["text"].startswith("<b>⏹ STOPPED</b>") for edit in api.edits)
    assert not any("THE INSPECTION ENDED" in edit["text"] for edit in api.edits)


async def test_a_card_of_another_session_is_left_alone_when_an_inspection_ends(
    tmp_path: Path, wired
) -> None:
    """An agent's own card, open while the inspection ran, is not the
    inspection's to close."""
    from halyard.core.events import RiskLevel

    others: list = []

    async def meanwhile(_asked) -> None:
        agents = await channel._store.create(
            session_id="id-nav",
            agent_id="claude-code",
            project="alpha-engine",
            tool="Bash",
            command_summary="make e2e",
            command_full="make e2e",
            risk=RiskLevel.HIGH,
        )
        await channel.send_approval_request(agents)
        others.append(agents)

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(channel, runner, "cat /tmp/x.log", meanwhile=meanwhile)

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)
    [agents] = others

    assert await channel._store.resolution_of(agents.request_id) is None
    said = "\n".join(sent["text"] for sent in api.sent)
    assert "`make e2e`" not in said
    assert len([edit for edit in api.edits if "THE INSPECTION ENDED" in edit["text"]]) == 1


async def test_an_inspection_s_status_and_reads_reach_its_reply_and_its_record(
    tmp_path: Path, wired
) -> None:
    """Measured on Codex: two commands that ran — one a `grep` that found
    nothing — are in its stream, and the read still waiting on its card is
    not. The trace has all three, each once; the answer is incomplete."""
    import json
    import sqlite3

    from halyard.inspect_cli import shown
    from halyard.inspections import repeat as repeating

    channel, api, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    channel._keep_inspections = True
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(
        channel,
        runner,
        "cat /tmp/x.log",
        under="codex-thread-7",
        ran=(("cat other.txt", 0), ("rg -n seed src", 1)),
    )

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    [answered] = [s["text"] for s in api.sent if s["text"].startswith("<b>proof</b> · ")]
    assert answered.startswith("<b>proof</b> · incomplete · 3 reads, 1 did not run\n<pre>")
    assert answered.endswith(f"</pre>\n\n{ENDED_X}"), "the note from before, as it was"
    with sqlite3.connect(channel._database) as db:
        [(ident, status, trace)] = db.execute(
            "SELECT id, status, trace FROM inspection_runs"
        ).fetchall()
    assert status == "incomplete"
    assert [(r["what"], r["outcome"], r["detail"]) for r in json.loads(trace)["reads"]] == [
        ("cat other.txt", "ran", ""),
        ("rg -n seed src", "ran", "exit 1"),
        ("cat /tmp/x.log", "ended", "still waiting for approval when the inspection ended"),
    ]
    assert "what it printed" not in trace
    printed = shown(repeating.find(channel._database, ident))
    assert printed[1] == "Status: incomplete · 3 reads, 1 did not run"
    assert (
        "  ended  Bash  cat /tmp/x.log — still waiting for approval when the inspection ended"
        in printed
    )


async def test_a_transition_s_inspection_says_it_is_complete_in_its_message_and_the_chat(
    tmp_path: Path, wired
) -> None:
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )
    ReadsAsked(channel, runner, under="codex-thread-4", ran=(("cat other.txt", 0),))

    await channel._run_transition("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [(_, text)] = runner.sent
    assert "Inspection proof — NOTES/proof.md @ uncommitted · complete · 1 read:" in text
    assert any("<b>proof</b>: complete · 1 read" in sent["text"] for sent in api.sent)


async def test_a_transition_s_inspection_with_no_answer_says_it_is_unmeasured(
    tmp_path: Path, wired
) -> None:
    """And the transition goes on, as it did."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, discovery={"inspections": ("proof",), "to": "xrev"}
    )
    ReadsAsked(channel, runner, under="codex-thread-5", ran=(("cat other.txt", 0),), says=None)

    await channel._run_transition("discovery", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    [(_, text)] = runner.sent
    assert "Inspection proof — NOTES/proof.md @ uncommitted · unmeasured:" in text
    assert any(
        "<b>proof</b>: unmeasured — the model did not answer" in sent["text"] for sent in api.sent
    )


async def test_a_read_the_gate_decided_with_no_card_is_in_the_trace_once(
    tmp_path: Path, wired
) -> None:
    """A read let through is the stream's, said once; one refused is the
    gate's alone. Another session's is not the inspection's."""

    async def meanwhile(_asked) -> None:
        for session, command, allowed, why in (
            ("codex-thread-6", "git status", True, "pre-authorized (reads)"),
            ("codex-thread-6", "git commit -m x", False, "refused: agents do not commit here"),
            ("id-nav", "make e2e", False, "refused: agents do not commit here"),
        ):
            channel.decided_without_a_card(
                session_id=session,
                tool="Bash",
                command=command,
                tool_use_id=None,
                allowed=allowed,
                why=why,
            )

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    ReadsAsked(
        channel, runner, under="codex-thread-6", ran=(("git status", 0),), meanwhile=meanwhile
    )

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    said = "\n".join(sent["text"] for sent in api.sent)
    assert "<b>proof</b> · incomplete · 2 reads, 1 did not run" in said
    assert "`git commit -m x` (refused: agents do not commit here)" in said
    assert "make e2e" not in said


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
    inspections_in(channel, repo, tmp_path, proof="# proof")
    await channel.send_message(
        "id-nav",
        "All 42 tests passed.",
        Role.NAVIGATOR,
        agent_id="claude-code",
        project="alpha-engine",
    )
    said = last_said.last(channel._kept(CHAT, SAID_FILE), CHAT)
    (repo / "seed.txt").write_text("changed after the report\n")

    await channel._handle_callback(pressed_inspection("proof"))
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
    channel._forge_tokens = {"gitlab": "glpat-x"}
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
    inspections_in(channel, repo, tmp_path, proof="# proof")
    levels(channel)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("backend", "level::3")))

    await channel._handle_callback(pressed_inspection("proof"))
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
    inspections_in(channel, repo, tmp_path, proof="# proof")
    levels(channel)
    behind_a_tracker(channel, monkeypatch, Tracker(refuse=ForgeError("GitLab says 401")))

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    [asked] = runner.asked
    assert "- Work item: alpha-engine#281" in asked
    assert "- level:" not in asked


async def test_a_project_without_label_groups_never_asks_the_tracker(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Not everybody needs this, and whoever does not never pays for it."""
    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("level::3",)))

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    assert tracker.asked == []
    [asked] = runner.asked
    assert "- level:" not in asked


def a_suite(channel: TelegramChannel, monkeypatch, *, output: str = "1420 passed") -> list:
    """`test-fast` among the project's commands, and a run of it that answers at once."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands={"test-fast": "make test-fast"})
    ran: list = []

    def running(line, path, *, timeout, on_progress=None, stop=None):
        ran.append((line, path, timeout))
        return Result(ok=True, output=output, seconds=94.0, exit_code=0)

    monkeypatch.setattr(under_test.commands_running, "run", running)
    return ran


async def test_a_transition_runs_its_commands_and_says_how_they_went(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Where the project is, one after another, and before the checks — then
    the seat gets what they did, and the chat is told."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    ran = a_suite(channel, monkeypatch)

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == [("make test-fast", repo, 600.0)]
    [(_, text)] = runner.sent
    assert "Ran test-fast: make test-fast · exit 0 (passed) · 94s · last line: 1420 passed" in text
    assert any("<b>test-fast</b>: passed" in sent["text"] for sent in api.sent)
    assert channel._working == {}


def a_suite_that_does_not_run(channel: TelegramChannel, monkeypatch) -> list:
    """`test-fast` among the project's commands, and a run of it that never found
    what it was to run."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands={"test-fast": "make test-fast"})
    ran: list = []

    def running(line, path, *, timeout, on_progress=None, stop=None):
        ran.append(line)
        return Result(ok=False, output="make: *** No rule", seconds=0.1, exit_code=127)

    monkeypatch.setattr(under_test.commands_running, "run", running)
    return ran


async def test_a_transition_whose_command_did_not_run_stops_and_says_why(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Nothing is inspected or sent: green read into a run that never
    happened is worse than no message."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    ran = a_suite_that_does_not_run(channel, monkeypatch)

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == ["make test-fast"]
    assert runner.sent == []
    said = api.sent[-1]["text"]
    assert "stopped before reaching" in said
    assert "exit 127 (not run)" in said
    assert channel._working == {}


async def test_a_workflow_step_whose_command_did_not_run_holds_the_run(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Held where it is, as for any stop: fixed, the step is picked again."""
    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], commands=("test-fast",))
    channel._repositories["alpha-engine"] = replace(
        found, transitions={**found.transitions, "review": review}
    )
    a_suite_that_does_not_run(channel, monkeypatch)

    await started(channel)

    work = await channel._work_item(channel._repositories["alpha-engine"])
    run = flowing.current(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)
    assert runner.sent == []
    assert run is not None
    assert run.stopped == "review: a command did not run"
    assert run.taken() == {}
    assert "review: a command did not run" in api.sent[-1]["text"]


async def test_stop_ends_a_transition_s_command_and_the_transition_with_it(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """An e2e run going wrong was ended only by restarting Halyard. `/stop`
    in the project's chat ends it, and the transition stops as for any
    command that did not run."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands={"test-fast": "make e2e"})

    def running(line, path, *, timeout, on_progress=None, stop=None):
        stop.wait(5)
        return Result(ok=False, output="collecting", seconds=1.0, stopped=True)

    monkeypatch.setattr(under_test.commands_running, "run", running)

    going = asyncio.create_task(channel._run_transition("close", CHAT, None, f"tg:{APPROVER}"))
    for _ in range(200):
        if "alpha-engine" in channel._stops:
            break
        await asyncio.sleep(0.01)
    await channel._handle_message(
        {"message_id": 9, "from": {"id": int(APPROVER)}, "chat": {"id": CHAT}, "text": "/stop"}
    )
    await asyncio.wait_for(going, timeout=5)
    await settled(channel)

    said = [sent["text"] for sent in api.sent]
    assert any("ended, with everything it started" in text for text in said)
    assert any("stopped from the chat" in text and "(not run)" in text for text in said)
    assert runner.sent == []
    assert channel._stops == {}


async def test_a_transition_pressed_while_paused_runs_nothing(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Measured: `close` pressed while paused ran its tests and inspections,
    and only then said to send /resume. It is said first now."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    ran = a_suite(channel, monkeypatch)
    await channel._gate.pause(f"tg:{APPROVER}")

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == []
    assert runner.sent == []
    assert "did not start: Halyard is paused" in api.sent[-1]["text"]


async def test_a_transition_to_an_agent_with_no_session_runs_nothing(
    tmp_path: Path, wired, monkeypatch
) -> None:
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    ran = a_suite(channel, monkeypatch)
    runner.sessions.pop("alpha-engine-xreview")

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == []
    assert "session could not be found" in api.sent[-1]["text"]


async def test_a_workflow_step_that_could_not_be_delivered_holds_the_run(
    tmp_path: Path, wired
) -> None:
    from halyard import workflows as flowing
    from halyard.channels.telegram.adapter import WORKFLOW_FILE

    channel, _, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    await channel._gate.pause(f"tg:{APPROVER}")

    await started(channel)

    work = await channel._work_item(channel._repositories["alpha-engine"])
    run = flowing.current(channel._kept_for("alpha-engine", WORKFLOW_FILE), work)
    assert runner.sent == []
    assert run is not None
    assert run.stopped == "review: Halyard is paused. Send /resume first"


async def test_a_transition_with_commands_does_not_go_while_another_runs(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Two `make` runs in one directory fight over the same outputs. Nothing
    is handed on, and it can be pressed again."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    ran = a_suite(channel, monkeypatch)
    channel._working["alpha-engine"] = "test-all"

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == []
    assert runner.sent == []
    assert "did not go" in api.sent[-1]["text"]
    assert channel._working == {"alpha-engine": "test-all"}


def scoped(channel: TelegramChannel, monkeypatch) -> list:
    """An end-to-end suite taking its scope from the task's `ddd-scope` label,
    and a run of it that answers at once."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={"e2e-scope": "make test-e2e-scope SCOPE={label_groups.ddd-scope}"},
        label_groups={"ddd-scope": ("ddd:capstone", "ddd:refining", "ddd:all")},
    )
    ran: list = []

    def running(line, path, *, timeout=None, on_progress=None, stop=None):
        ran.append(line)
        return Result(ok=True, output="12 passed", seconds=3.0, exit_code=0)

    monkeypatch.setattr(under_test.commands_running, "run", running)
    return ran


def picked(value: str) -> dict:
    return pressed_transition("pick", value)


async def test_a_command_takes_its_value_from_the_tasks_labels(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Every label of the group the task carries, in the group's order."""
    channel, _, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("e2e-scope",), "to": "xrev"}
    )
    ran = scoped(channel, monkeypatch)
    behind_a_tracker(channel, monkeypatch, Tracker(labels=("ddd:refining", "ddd:capstone")))

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == ["make test-e2e-scope SCOPE=capstone,refining"]
    [(_, text)] = runner.sent
    assert "Ran e2e-scope: make test-e2e-scope SCOPE=capstone,refining · exit 0" in text


async def test_a_task_without_the_label_is_asked_for_one_and_it_goes_on_the_task(
    tmp_path: Path, wired, monkeypatch
) -> None:
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("e2e-scope",), "to": "xrev"}
    )
    ran = scoped(channel, monkeypatch)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("backend",)))

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert (ran, runner.sent) == ([], []), "nothing runs with the braces still in the line"
    asking = api.sent[-1]
    assert "takes the task's <b>ddd-scope</b> label" in asking["text"]
    keys = [key["text"] for key in asking["reply_markup"]["inline_keyboard"][0]]
    assert keys == ["ddd:capstone", "ddd:refining", "ddd:all"]

    await channel._handle_callback(picked("hclose>0>1"))
    await settled(channel)

    assert tracker.added == [(281, "ddd:refining")]
    assert ran == ["make test-e2e-scope SCOPE=refining"]
    assert len(runner.sent) == 1


async def test_a_branch_naming_no_task_keeps_the_pick_for_this_work(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """Nothing to put the label on, so it is used for this work until Halyard
    restarts — the second press is not asked again."""
    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("e2e-scope",), "to": "xrev"}
    )
    ran = scoped(channel, monkeypatch)

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)
    assert "names no task" in api.sent[-1]["text"]

    await channel._handle_callback(picked("hclose>0>2"))
    await settled(channel)
    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert ran == ["make test-e2e-scope SCOPE=all", "make test-e2e-scope SCOPE=all"]
    # The card it was picked on is spent, saying which.
    assert any(e["message_id"] == 5 and e["text"] == "☑️ ddd:all" for e in api.edits)


async def test_command_fills_the_label_in_as_a_transition_does(
    tmp_path: Path, wired, monkeypatch
) -> None:
    channel, api, _, _ = wired
    ran = scoped(channel, monkeypatch)
    behind_a_tracker(channel, monkeypatch, Tracker(labels=("ddd:all",)))
    channel._said_path = tmp_path / "last-said.json"

    await channel._run_command("e2e-scope", CHAT, None)
    await settled(channel)

    assert ran == ["make test-e2e-scope SCOPE=all"]
    assert any("Running <code>make test-e2e-scope SCOPE=all</code>" in s["text"] for s in api.sent)


async def test_a_workflow_step_waits_for_the_label_and_the_pick_sends_it(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """A label is somebody's to pick, so the run stops there — and the tap
    puts it on the task and sends the step."""
    from halyard.core.config_file import Step, Workflows

    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("e2e-scope",), "to": "xrev"}
    )
    ran = scoped(channel, monkeypatch)
    tracker = behind_a_tracker(channel, monkeypatch, Tracker())
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        workflows=Workflows(
            steps={"close": Step(name="close", transition="close", seat="xrev")},
            flows={"level3": ("close",)},
        ),
    )

    await channel._run_workflow("level3", CHAT, None, f"tg:{APPROVER}", start=0)
    await settled(channel)

    assert runner.sent == []
    asking = api.sent[-1]
    assert asking["text"].startswith("⏸ <b>level3</b> 1/1 · close — ")
    keys = [key["text"] for row in asking["reply_markup"]["inline_keyboard"] for key in row]
    assert "ddd:capstone" in keys and "⏹ Stop the workflow" in keys

    await channel._handle_callback(picked("wlevel3>0>0"))
    await settled(channel)

    assert tracker.added == [(281, "ddd:capstone")]
    assert ran == ["make test-e2e-scope SCOPE=capstone"]
    [(session, text)] = runner.sent
    assert session == "id-rev"
    assert "- Workflow: level3 · step 1 of 1 · close" in text


async def test_an_inspection_runs_on_the_default_runtime_whichever_chat_asks(
    tmp_path: Path, wired
) -> None:
    """Asked from an opencode agent's chat, it still runs where its model and
    effort mean what they say. Choosing by the chat meant the default only while
    nothing else could take a turn of its own."""
    channel, _, runner, repo = wired
    other = FakeRunner()
    channel._runners["opencode"] = other
    channel._seats = [replace(seat, runtime="opencode") for seat in channel._seats]
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    assert other.asked == []
    assert len(runner.asked) == 1


async def test_an_inspection_s_session_is_marked_as_halyard_s_own(tmp_path: Path, wired) -> None:
    """Before its turn begins, so its reply is not relayed as a stranger's and
    it is never taken for a seat at work."""
    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    [session] = runner.session_ids
    assert channel._registry.own(session) == "proof"


async def test_pressing_a_check_runs_that_one_over_the_last_reply(tmp_path: Path, wired) -> None:
    """Its own instructions, the whole reply, and what Halyard can see."""
    from halyard.channels.telegram.adapter import INSPECTION_MODEL

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof: find the evidence", claims="# claims")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    [asked] = runner.asked
    assert asked.startswith("# proof: find the evidence")
    assert "All 42 tests passed." in asked
    assert "alpha-engine#281" in asked
    assert runner.models == [INSPECTION_MODEL]
    assert api.sent[-1]["text"].startswith("<b>proof</b>")


async def test_an_inspection_runs_on_its_own_model_and_the_rest_on_the_machine_s(
    tmp_path: Path, wired
) -> None:
    """`HALYARD_INSPECTION_EFFORT: max` for every inspection, and one that says
    `model: opus` where it is described — the effort it leaves unsaid is the
    machine's still."""
    from halyard.core.config_file import ModelChoice

    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof", bounded="# bounded context")
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, inspection_models={"bounded": ModelChoice(model="opus")}
    )
    channel._inspection_model = ModelChoice("sonnet", "max")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)
    await channel._handle_callback(pressed_inspection("bounded"))
    await settled(channel)

    assert list(zip(runner.models, runner.efforts, strict=True)) == [
        ("sonnet", "max"),
        ("opus", "max"),
    ]


async def test_an_inspection_on_a_gpt_model_runs_on_codex_and_the_rest_where_they_did(
    tmp_path: Path, wired
) -> None:
    """The model says the runtime: `gpt-6.1-sol` is Codex's, so that inspection
    takes its turn there, and the one on the machine's `sonnet` stays put."""
    from halyard.channels.telegram.adapter import INSPECTION_MODEL
    from halyard.core.config_file import ModelChoice

    channel, _, runner, repo = wired
    codex = FakeRunner(says="durum: temiz")
    channel._runners["codex"] = codex
    inspections_in(channel, repo, tmp_path, proof="# proof", bounded="# bounded context")
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, inspection_models={"bounded": ModelChoice("gpt-6.1-sol", "high")}
    )

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)
    await channel._handle_callback(pressed_inspection("bounded"))
    await settled(channel)

    assert runner.models == [INSPECTION_MODEL]
    assert (codex.models, codex.efforts) == (["gpt-6.1-sol"], ["high"])


async def test_an_inspection_whose_model_nothing_here_runs_says_so(tmp_path: Path, wired) -> None:
    from halyard.core.config_file import ModelChoice

    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    channel._inspection_model = ModelChoice("gpt-5.6-terra")

    await channel._handle_callback(pressed_inspection("proof"))
    await settled(channel)

    assert runner.asked == []
    assert "No runtime here can take a turn on <b>gpt-5.6-terra</b>" in api.sent[-1]["text"]


def test_what_the_machine_leaves_unsaid_is_the_channel_s_own_model() -> None:
    """Only an effort set, as `HALYARD_INSPECTION_EFFORT: max` alone would."""
    from halyard.channels.telegram.adapter import INSPECTION_MODEL
    from halyard.core.config_file import ModelChoice

    channel = TelegramChannel(
        api=FakeApi(),
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=AuditLog([]),
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        inspection_model=ModelChoice(effort="max"),
    )

    assert channel._inspection_model == ModelChoice(INSPECTION_MODEL, "max")


async def test_a_check_named_after_the_command_runs_with_the_note(tmp_path: Path, wired) -> None:
    """`/inspect proof delivery` — the note says which stage the reply belongs to."""
    channel, _, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_message(typed("/inspect proof delivery"))
    await settled(channel)

    [asked] = runner.asked
    assert "delivery" in asked


async def test_a_check_nobody_defined_is_said_and_the_rest_offered(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_inspection("nope", CHAT, None)

    assert "no inspection called <b>nope</b>" in api.sent[-2]["text"]
    assert api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert runner.asked == []


async def test_a_check_the_model_could_not_answer_is_said_not_skipped(
    tmp_path: Path, wired
) -> None:
    """A missing answer reads exactly like a clean one."""
    channel, api, runner, repo = wired
    runner.says = None
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_inspection("proof", CHAT, None)

    assert "unmeasured" in api.sent[-1]["text"]


async def test_a_project_without_checks_says_so(wired) -> None:
    channel, api, runner, _ = wired

    await channel._run_inspection("", CHAT, None)

    assert "no <code>inspections:</code>" in api.sent[-1]["text"]
    assert runner.asked == []


async def test_nothing_is_checked_before_anything_was_said(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")
    channel._said_path = tmp_path / "elsewhere" / "last-said.json"

    await channel._run_inspection("", CHAT, None)

    assert "nothing to inspect" in api.sent[-1]["text"]
    assert runner.asked == []


async def test_inspect_answers_from_a_phone(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_message(typed("/inspect"))
    await settled(channel)

    assert api.sent[-1]["reply_markup"]["inline_keyboard"]
    assert runner.asked == []


async def test_an_inspection_run_by_hand_is_kept_under_its_tokens_id(tmp_path: Path, wired) -> None:
    """What it was given and what it said, in the database beside the tokens,
    under the id the turn ran under."""
    import sqlite3

    channel, _, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    channel._keep_inspections = True
    inspections_in(channel, repo, tmp_path, proof="# proof: find the evidence")

    await channel._run_inspection("proof delivery", CHAT, None)
    await settled(channel)

    with sqlite3.connect(channel._database) as db:
        [row] = db.execute(
            "SELECT id, project, work, inspection, transition, runtime, model, note, input, "
            "answer, outcome, step FROM inspection_runs"
        ).fetchall()
    (
        ident,
        project,
        work,
        inspection,
        transition,
        runtime,
        model,
        note,
        asked,
        answer,
        outcome,
        step,
    ) = row
    assert ident == runner.session_ids[0]
    assert (project, work, inspection, transition) == (
        "alpha-engine",
        "alpha-engine#281",
        "proof",
        None,
    )
    assert (runtime, model, note) == ("claude-code", "sonnet", "delivery")
    assert asked == runner.asked[0]
    assert "All 42 tests passed." in asked
    assert (answer, outcome, step) == ("loader stub and seed tweak", "answered", None)


async def test_an_inspection_a_workflow_step_ran_is_kept_against_that_step(
    tmp_path: Path, wired
) -> None:
    """So it joins `workflow_steps`: the run, the step, its phase and round."""
    import sqlite3

    channel, _, runner, repo = wired
    channel._database = tmp_path / "halyard.db"
    channel._keep_inspections = True
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    (repo / "NOTES" / "proof.md").write_text("# proof")
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        inspections={"proof": Path("NOTES/proof.md")},
        transitions={
            **found.transitions,
            "review": replace(found.transitions["review"], inspections=("proof",)),
        },
    )

    await started(channel)

    with sqlite3.connect(channel._database) as db:
        [row] = db.execute(
            "SELECT inspection, transition, workflow_run, step, phase, round FROM inspection_runs"
        ).fetchall()
    inspection, transition, workflow_run, step, phase, number = row
    assert (inspection, transition, step, phase, number) == ("proof", "review", "review", None, 1)
    assert re.fullmatch(r"[0-9a-f]{8}", workflow_run), "kept under the run's own name"


async def test_inspections_are_not_kept_unless_asked_for(tmp_path: Path, wired) -> None:
    """The one place Halyard would keep text, so it waits to be turned on."""
    channel, _, _, repo = wired
    channel._database = tmp_path / "halyard.db"
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._run_inspection("proof", CHAT, None)
    await settled(channel)

    assert not channel._database.exists()


async def test_keeping_an_inspection_holds_nobody_up(tmp_path: Path, wired, monkeypatch) -> None:
    """The row is written off to one side: a database busy with the audit log
    must not keep an answer from reaching anybody."""
    import threading

    from halyard import inspections
    from halyard.channels.telegram.adapter import _Keeping

    channel, _, _, _ = wired
    channel._database = tmp_path / "halyard.db"
    released = threading.Event()
    written: list[str] = []

    def slow_keep(path, kept, **_) -> None:
        released.wait(5)
        written.append(kept.name)

    monkeypatch.setattr(inspections.record, "keep", slow_keep)
    keeper = _Keeping(channel, project="alpha-engine", work="alpha-engine#281")

    await asyncio.wait_for(keeper.keep(a_kept_inspection()), timeout=1)

    assert written == [], "it returned before the write finished"
    released.set()
    await settled(channel)
    assert written == ["proof"]


def a_kept_inspection():
    from datetime import UTC, datetime

    from halyard import inspections

    return inspections.Kept(
        session="sess-1",
        at=datetime(2026, 9, 23, 18, 0, tzinfo=UTC),
        name="proof",
        path=Path("NOTES/proof.md"),
        version="3e8c847",
        transition="",
        model="sonnet",
        asked="# proof",
        context=(),
        note="",
        answer="proof · no finding",
        why="",
        finding=None,
        took=1.0,
    )


async def test_the_names_inspections_had_before_still_work(tmp_path: Path, wired) -> None:
    """`/checks` and the buttons it left in chats, until 2026-09-23."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, proof="# proof")

    await channel._handle_message(typed("/checks"))
    await settled(channel)
    offered = api.sent[-1]["reply_markup"]["inline_keyboard"]
    await channel._handle_callback(pressed_inspection("proof", what="check"))
    await settled(channel)

    assert offered
    assert len(runner.asked) == 1


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
    `commands:`, and `validate:` naming it — read as the commit option `review`."""
    from halyard.core.config_file import CommitOption

    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={**found.commands, "check": command} if command else found.commands,
        commit_options={"review": CommitOption("review", commands=("check",))} if command else {},
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

    assert "<b>review</b> — running <code>true</code>" in api.sent[0]["text"]


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
    assert "NO ROUND SENT" in api.edits[-1]["text"], "the card says so too"


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
    assert "NO ROUND SENT" in api.edits[-1]["text"], "the card says so too"


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

    Split by button rather than by a list of exceptions: a rule naming the
    paths that skip the gate has to be maintained against a repository that
    keeps growing, and gets it wrong quietly. A button is pressed at the moment
    somebody already knows which they meant.
    """
    from halyard.channels.telegram import cards

    channel, api, _, repo = wired
    demands(channel, "echo ran >> " + str(repo.parent / "ran.txt"))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))
    await tap(channel, pressed_transition("commit", cards.PLAIN_COMMIT))

    assert not (repo.parent / "ran.txt").exists()
    assert "running" not in " ".join(m["text"] for m in api.sent)
    assert len(channel._proposals) == 1
    assert any(e["message_id"] == 5 and e["text"] == "☑️ Commit" for e in api.edits)


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


# --- commit options: a project's own checks, from /commit's card -------------


def offers(channel: TelegramChannel, tmp_path: Path, **options) -> None:
    """Give the project commands, inspections and commit options, as
    `halyard.yaml` would. A command writes where it ran outside the repository,
    so running it changes no file a commit would take."""
    from halyard.core.config_file import CommitOption

    log = tmp_path / "ran.log"
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={
            "test-fast": f"echo test-fast >> {log} && echo '12 passed'",
            "lint": f"echo lint >> {log}",
            "broken": f"echo broken >> {log} && echo 'FAIL: two tests broke' && exit 1",
            "skipped": f"echo skipped >> {log} && exit 2",
            "writes": "echo made > made.py",
            **found.commands,
        },
        commands_not_run={"skipped": (2,)},
        commit_options={
            name: CommitOption(name, commands=tuple(ran), inspections=tuple(looked))
            for name, (ran, looked) in options.items()
        },
    )


def ran_here(tmp_path: Path) -> list[str]:
    log = tmp_path / "ran.log"
    return log.read_text().split() if log.exists() else []


def buttons_of(sent: dict) -> list[str]:
    return [key["text"] for row in sent["reply_markup"]["inline_keyboard"] for key in row]


async def test_commit_offers_the_plain_commit_and_each_option(tmp_path: Path, wired) -> None:
    """Nothing runs before somebody has said which."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, destructive="# destructive")
    offers(
        channel, tmp_path, fast=(["test-fast"], ["destructive"]), wide=(["test-fast", "lint"], [])
    )
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    card = api.sent[-1]
    assert buttons_of(card) == ["✅ Commit", "🧪 fast", "🧪 wide", "✖️ Cancel"]
    assert "✅ <b>Commit</b> — checks nothing" in card["text"]
    assert "🧪 <b>fast</b> — runs test-fast · inspects destructive" in card["text"]
    assert "🧪 <b>wide</b> — runs test-fast, lint" in card["text"]
    assert (ran_here(tmp_path), runner.asked, len(channel._proposals)) == ([], [], 0)


async def test_a_project_without_options_is_proposed_a_commit_at_once(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit"))

    assert len(channel._proposals) == 1
    assert "how?" not in " ".join(m["text"] for m in api.sent)


async def test_an_option_runs_its_commands_then_inspects_the_change(tmp_path: Path, wired) -> None:
    """In order, and the inspections read the change about to be committed —
    not the chat's last reply, which may be about something else."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, destructive="# destructive")
    offers(channel, tmp_path, fast=(["test-fast", "lint"], ["destructive"]))
    wrote(repo, "loader.py", "x = 1\n")
    wrote(repo, "seed.txt", "b\n")

    await deliver(channel, typed("/commit"))
    await tap(channel, pressed_transition("commit", "fast"))

    assert any(e["message_id"] == 5 and e["text"] == "☑️ fast" for e in api.edits)
    assert ran_here(tmp_path) == ["test-fast", "lint"]
    inspected, written = runner.asked
    assert "# destructive" in inspected
    text = inspected.split("Text to inspect:", 1)[1]
    assert "Tracked files, from `git diff --stat HEAD`:" in text
    assert " seed.txt | 2 +-" in text
    assert (
        "Untracked files, which `git add -A` takes with them:\nNOTES/destructive.md\nloader.py"
        in text
    )
    assert "All 42 tests passed." not in inspected, "the chat's last reply is not what it reads"
    envelope = inspected.split("Envelope:", 1)[1].split("Text to inspect:", 1)[0]
    assert "- Project: alpha-engine" in envelope
    assert "- Ran test-fast: " in envelope and "last line: 12 passed" in envelope
    assert "Text to inspect" not in written, "then the message is written"

    said = [m["text"] for m in api.sent]
    assert any(text.startswith("<b>destructive</b> · ") for text in said), "its answer is said"
    card = api.sent[-1]["text"]
    assert "✅ fast · test-fast passed · " in card
    assert "✅ fast · lint passed · " in card
    assert "🔎 fast · destructive complete" in card
    assert card.index("🔎 fast") < card.index("<pre>alpha-engine#281")
    assert len(channel._proposals) == 1


async def test_an_option_can_be_named_when_commit_is_typed(tmp_path: Path, wired) -> None:
    channel, _, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit FAST"))

    assert ran_here(tmp_path) == ["test-fast"]
    assert len(channel._proposals) == 1


async def test_an_option_nobody_defined_is_said_and_the_card_offered(tmp_path: Path, wired) -> None:
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit wide"))

    assert "has no commit option called <b>wide</b>" in api.sent[-2]["text"]
    assert buttons_of(api.sent[-1]) == ["✅ Commit", "🧪 fast", "✖️ Cancel"]
    assert ran_here(tmp_path) == []


async def test_review_and_commit_offers_only_the_options(tmp_path: Path, wired) -> None:
    """It asks for the checks, so the plain Commit is not among them."""
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []), wide=(["lint"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit"))

    assert buttons_of(api.sent[-1]) == ["🧪 fast", "🧪 wide", "✖️ Cancel"]
    assert ran_here(tmp_path) == []


async def test_an_option_mistyped_is_never_taken_for_the_one_there_is(
    tmp_path: Path, wired
) -> None:
    """`/review_and_commit` runs a project's one option — but not in place of
    another name somebody typed."""
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/review_and_commit wide"))

    assert "has no commit option called <b>wide</b>" in api.sent[-2]["text"]
    assert buttons_of(api.sent[-1]) == ["🧪 fast", "✖️ Cancel"]
    assert ran_here(tmp_path) == []


async def test_a_project_without_options_says_so_for_a_name(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit fast"))

    assert "has no commit option called <b>fast</b>" in api.sent[-1]["text"]
    assert len(channel._proposals) == 0


async def test_a_failing_command_stops_the_option_before_its_inspections(
    tmp_path: Path, wired
) -> None:
    """A failed check is a fact with nothing in it to weigh: no card, and
    nothing after it runs."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, destructive="# destructive")
    offers(channel, tmp_path, fast=(["broken", "lint"], ["destructive"]))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit fast"))

    assert ran_here(tmp_path) == ["broken"]
    assert runner.asked == []
    said = api.sent[-1]["text"]
    assert said.startswith("🚫 <b>broken</b> failed after ")
    assert "so <b>fast</b> stopped there. Nothing was committed." in said
    assert "FAIL: two tests broke" in said
    assert len(channel._proposals) == 0


async def test_a_command_that_did_not_run_stops_the_option(tmp_path: Path, wired) -> None:
    channel, api, runner, repo = wired
    offers(channel, tmp_path, fast=(["skipped", "lint"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit fast"))

    assert ran_here(tmp_path) == ["skipped"]
    assert api.sent[-1]["text"].startswith("🚫 <b>skipped</b> did not run (exit 2), so")
    assert (runner.asked, len(channel._proposals)) == ([], 0)


async def test_documentation_alone_runs_nothing_and_says_so(tmp_path: Path, wired) -> None:
    """No test suite or inspection of code has anything to say about prose. The
    warnings still read what was written."""
    channel, api, runner, repo = wired
    inspections_in(channel, repo, tmp_path, destructive="# destructive")
    offers(channel, tmp_path, fast=(["test-fast"], ["destructive"]))
    wrote(repo, "README.md", "# notes\n")

    await deliver(channel, typed("/commit fast"))

    assert ran_here(tmp_path) == []
    assert all("Text to inspect" not in asked for asked in runner.asked)
    assert "📄 Documentation only — <b>fast</b> runs nothing for it." in api.sent[-2]["text"]
    card = api.sent[-1]["text"]
    assert "📄 fast · documentation only, nothing run" in card
    assert "281 appears nowhere" in card
    assert len(channel._proposals) == 1


async def test_files_changed_after_the_check_are_not_committed(tmp_path: Path, wired) -> None:
    """A commit takes the tree as it is at the tap. One that moved since its
    option checked it would commit what nobody checked."""
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit fast"))
    wrote(repo, "later.py", "y = 2\n")

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert commit_count(repo) == 1
    assert len(channel._proposals) == 0, "spent: a fresh /commit checks the files as they are"
    assert api.sent[-1]["text"].startswith(
        "🚫 The files changed since they were checked, so nothing was committed."
    )
    assert "NOT COMMITTED — the files changed since they were checked" in api.edits[-1]["text"]


async def test_files_left_as_they_were_checked_are_committed(tmp_path: Path, wired) -> None:
    channel, _, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit fast"))

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert commit_count(repo) == 2
    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"


async def test_a_change_the_files_had_after_documentation_alone_is_not_committed(
    tmp_path: Path, wired
) -> None:
    """Nothing ran for the prose, and code arriving afterwards would be
    committed unchecked."""
    channel, _, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "README.md", "# notes\n")
    await deliver(channel, typed("/commit fast"))
    wrote(repo, "loader.py", "x = 1\n")

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert commit_count(repo) == 1


async def test_a_plain_commit_is_not_held_to_the_files_it_showed(wired) -> None:
    """Nothing checked them, so the tap commits the branch as it is, as it
    always has."""
    channel, _, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit"))
    wrote(repo, "later.py", "y = 2\n")

    await tap(channel, press(only_handle(channel), commit_card.MAKE))

    assert commit_count(repo) == 2


async def test_files_changed_while_they_were_checked_get_no_card(tmp_path: Path, wired) -> None:
    """A command that writes into the tree — a formatter — or an agent writing
    meanwhile: what was checked is not what a commit would take."""
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["writes"], []))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit fast"))

    assert api.sent[-1]["text"].startswith("🚫 The files changed while <b>fast</b> checked them")
    assert len(channel._proposals) == 0


async def test_rewording_keeps_what_the_option_checked_on_the_card(tmp_path: Path, wired) -> None:
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")
    await deliver(channel, typed("/commit fast"))
    handle = only_handle(channel)
    await tap(channel, press(handle, commit_card.REWRITE))

    await deliver(channel, replying("power gen fixes", api.sent[-1]["text"]))

    assert "✅ fast · test-fast passed · " in api.sent[-1]["text"]
    assert channel._proposals.peek(handle).checked_content is not None


async def test_one_project_command_at_a_time_counts_an_option(tmp_path: Path, wired) -> None:
    channel, api, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    wrote(repo, "loader.py", "x = 1\n")
    channel._working["alpha-engine"] = "test-all"

    await deliver(channel, typed("/commit fast"))

    assert "<b>test-all</b> is still running in <b>alpha-engine</b>" in api.sent[-1]["text"]
    assert (ran_here(tmp_path), len(channel._proposals)) == ([], 0)
    assert channel._working == {"alpha-engine": "test-all"}, "left as it was"


async def test_an_option_s_commands_hold_the_project_while_they_run(tmp_path: Path, wired) -> None:
    """So `/stop` can end one, and nothing else runs beside it."""
    channel, _, _, repo = wired
    offers(channel, tmp_path, fast=(["test-fast"], []))
    seen: list[dict] = []
    real = channel._command_run

    async def watched(project, line, path, **more):
        seen.append(dict(channel._working))
        return await real(project, line, path, **more)

    channel._command_run = watched
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit fast"))

    assert seen == [{"alpha-engine": "commit fast"}]
    assert channel._working == {}


async def test_a_command_taking_a_label_asks_for_it_and_the_pick_runs_the_option(
    tmp_path: Path, wired, monkeypatch
) -> None:
    from halyard.core.config_file import CommitOption

    channel, api, _, repo = wired
    ran = scoped(channel, monkeypatch)
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(
        found, commit_options={"wide": CommitOption("wide", commands=("e2e-scope",))}
    )
    tracker = behind_a_tracker(channel, monkeypatch, Tracker(labels=("backend",)))
    wrote(repo, "loader.py", "x = 1\n")

    await deliver(channel, typed("/commit wide"))

    assert ran == []
    asking = api.sent[-1]
    assert "takes the task's <b>ddd-scope</b> label" in asking["text"]
    refining = asking["reply_markup"]["inline_keyboard"][0][1]
    assert refining["text"] == "ddd:refining"

    await tap(channel, {**picked(""), "data": refining["callback_data"]})

    assert tracker.added == [(281, "ddd:refining")]
    assert ran == ["make test-e2e-scope SCOPE=refining"]
    assert "✅ wide · e2e-scope passed · " in api.sent[-1]["text"]


def test_commit_is_registered_and_telegram_will_accept_it() -> None:
    """One invalid name and Telegram rejects the whole list, so every command
    is silently missing from the menu. `/review_and_commit` is off it, now
    that `/commit` offers the checks, and still answered — so no prompt may
    take its name."""
    from halyard.channels.telegram.adapter import COMMANDS, reserved_names

    names = {name for name, _ in COMMANDS}
    assert "commit" in names
    assert "review_and_commit" not in names
    assert "review_and_commit" in reserved_names()
    for name, description in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert len(description) <= 256, name


async def test_a_workflow_step_says_on_its_run_card_what_it_is_doing(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """The run's one card says what its step is doing — its command with its
    time, its inspections counted, then its agent — rather than a message of
    its own every 45 seconds while the command prints."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    channel, api, runner, repo = wired
    flow_in(channel, repo, tmp_path, runner, **TWO_STEPS)
    inspections_in(channel, repo, tmp_path, proof="# proof")
    found = channel._repositories["alpha-engine"]
    review = replace(found.transitions["review"], commands=("test-fast",), inspections=("proof",))
    channel._repositories["alpha-engine"] = replace(
        found,
        commands={"test-fast": "make test-fast"},
        transitions={**found.transitions, "review": review},
    )

    def running(line, path, *, timeout, on_progress=None, stop=None):
        on_progress(60.0, "collected 12 items")
        return Result(ok=True, output="12 passed", seconds=61.0, exit_code=0)

    monkeypatch.setattr(under_test.commands_running, "run", running)

    await started(channel)

    [card] = [sent for sent in api.sent if "🤝 <b>review</b>" in sent["text"]]
    lines = [
        edit["text"].removeprefix(card["text"] + "\n\n")
        for edit in api.edits
        if edit["text"].startswith(card["text"] + "\n\n⏳")
    ]
    assert lines[0] == "⏳ review — running test-fast · under a minute"
    assert "⏳ review — inspecting · 0 of 1 · under a minute" in lines
    assert "⏳ review — inspecting · 1 of 1 · under a minute" in lines
    assert "⏳ review — on its way to xrev · under a minute" in lines
    assert lines[-1] == "⏳ review — waiting for xrev · under a minute"
    assert not any(sent["text"].startswith("… <b>test-fast</b>") for sent in api.sent)
    assert runner.sent, "and the step went"


async def test_a_command_from_transition_by_hand_still_says_how_it_is_getting_on(
    tmp_path: Path, wired, monkeypatch
) -> None:
    """A transition pressed by hand has no run card: its command says how it
    is getting on in a message of its own, as before."""
    from halyard.channels.telegram import adapter as under_test
    from halyard.commands import Result

    channel, api, runner, repo = wired
    transitions_in(
        channel, repo, tmp_path, runner, close={"commands": ("test-fast",), "to": "xrev"}
    )
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, commands={"test-fast": "make test-fast"})

    def running(line, path, *, timeout, on_progress=None, stop=None):
        on_progress(60.0, "collected 12 items")
        return Result(ok=True, output="12 passed", seconds=61.0, exit_code=0)

    monkeypatch.setattr(under_test.commands_running, "run", running)

    await channel._run_transition("close", CHAT, None, f"tg:{APPROVER}")
    await settled(channel)

    assert any(
        sent["text"].startswith("… <b>test-fast</b>") and "collected 12 items" in sent["text"]
        for sent in api.sent
    )
    assert not any("⏳" in (edit["text"] or "") for edit in api.edits)
