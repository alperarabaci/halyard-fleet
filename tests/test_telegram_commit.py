"""Tests for `/commit` — the Telegram half.

`tests/test_commits.py` covers what git is asked and how its answers are read.
This covers the part that can commit something nobody agreed to: which button
does it, who is allowed to press it, and what happens when it is pressed twice.

The repository is real, so a passing test here means a commit actually landed.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from halyard.channels.telegram import cards
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.core.approvals import ApprovalStore
from halyard.core.audit import AuditLog, JsonlAuditSink
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

    async def ask(self, text: str, *, model: str | None = None, **kwargs) -> str | None:
        self.asked.append(text)
        self.models.append(model)
        if self.says is None:
            raise RuntimeError("no model today")
        return self.says


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
    channel = TelegramChannel(
        api=api,
        store=ApprovalStore(ttl=timedelta(minutes=5)),
        audit=audit,
        chat_id=CHAT,
        authorized_user_ids=frozenset({APPROVER}),
        seats=[Seat(label="nav", runtime="claude-code", chat=CHAT, project="alpha-engine")],
        runners={"claude-code": runner},
        repositories={"alpha-engine": repo},
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
        "data": cards.commit_data(handle, action),
        "message": {"message_id": message_id, "chat": {"id": CHAT}},
    }


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)


def only_handle(channel: TelegramChannel) -> str:
    handles = list(channel._proposals)
    assert len(handles) == 1, handles
    return handles[0]


def subject(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%s").strip()


def commit_count(repo: Path) -> int:
    return len(git(repo, "log", "--format=%h").splitlines())


# --- proposing --------------------------------------------------------------


async def test_a_card_offers_the_message_and_what_it_would_commit(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "def load():\n    return 1\n")

    await channel._handle_message(typed("/commit"))

    card = api.sent[-1]
    assert "alpha-engine#281 loader stub and seed tweak" in card["text"]
    assert "281-power-gen-minor-fixes" in card["text"]
    assert "loader.py" in card["text"]
    assert card["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Commit"
    # Proposed, not committed. Nothing has happened to the repository yet.
    assert commit_count(repo) == 1


async def test_the_model_is_asked_with_the_house_style_and_the_cheap_model(wired) -> None:
    channel, _, runner, repo = wired
    stage(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert runner.models == ["sonnet"]
    assert "alpha-engine#279 p2" in runner.asked[0]


async def test_nothing_staged_is_said_rather_than_committed(wired) -> None:
    channel, api, _, repo = wired
    (repo / "unstaged.txt").write_text("not added\n")

    await channel._handle_message(typed("/commit"))

    assert "Nothing is staged" in api.sent[-1]["text"]
    assert channel._proposals == {}
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
        await channel._handle_message(typed("/commit"))
        assert "do not know which repository" in api.sent[-1]["text"]
    finally:
        await audit.close()


async def test_a_model_that_cannot_be_reached_still_offers_the_reference(wired) -> None:
    """Failing soft. The branch already names the issue, and Rewrite is one tap
    away — losing the commit because a model was busy would be the worse end."""
    channel, api, runner, repo = wired
    runner.says = None
    stage(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert "alpha-engine#281" in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


# --- committing -------------------------------------------------------------


async def test_pressing_commit_makes_the_commit(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), cards.MAKE))

    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"
    assert commit_count(repo) == 2
    assert "COMMITTED" in api.edits[-1]["text"]


async def test_pressing_commit_twice_makes_one_commit(wired) -> None:
    """There is no nonce here — the proposal is dropped as it is used, and that
    is what a second tap runs into."""
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    await channel._handle_callback(press(handle, cards.MAKE))
    await channel._handle_callback(press(handle, cards.MAKE))

    assert commit_count(repo) == 2
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_cancel_leaves_the_repository_alone(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), cards.DROP))

    assert commit_count(repo) == 1
    assert "CANCELLED" in api.edits[-1]["text"]
    assert channel._proposals == {}


async def test_somebody_else_pressing_commit_commits_nothing(wired) -> None:
    """The card is visible to a whole group. Checked exactly as an approval is."""
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), cards.MAKE, user=INTRUDER))

    assert commit_count(repo) == 1
    assert len(channel._proposals) == 1
    assert api.edits == []


async def test_a_proposal_left_too_long_is_not_committable(wired) -> None:
    """The card describes a staging area as it was. A button tapped tomorrow
    would commit whatever is staged then, under a message written for this."""
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    channel._proposals[handle] = replace(
        channel._proposals[handle], at=channel._clock() - timedelta(hours=2)
    )
    channel._forget_stale_proposals()
    await channel._handle_callback(press(handle, cards.MAKE))

    assert commit_count(repo) == 1
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_git_refusing_is_reported_rather_than_swallowed(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    # Unstaged behind the card's back, so `git commit` has nothing to do.
    git(repo, "reset", "-q")

    await channel._handle_callback(press(handle, cards.MAKE))

    assert commit_count(repo) == 1
    assert "git refused" in api.sent[-1]["text"]


# --- rewriting --------------------------------------------------------------


async def test_rewrite_asks_for_a_message_and_commits_nothing(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    await channel._handle_callback(press(handle, cards.REWRITE))

    assert api.sent[-1]["reply_markup"] == {"force_reply": True}
    assert handle in api.sent[-1]["text"]
    assert commit_count(repo) == 1
    # Kept, not taken: the sentence still has to find it.
    assert handle in channel._proposals


async def test_a_typed_message_replaces_the_wording_without_committing(wired) -> None:
    """One thing in this flow commits, and it is the Commit button. A typo
    typed on a phone must not be a commit nobody agreed to."""
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, cards.REWRITE))
    asked = api.sent[-1]["text"]

    await channel._handle_message(replying("power gen minor fixes", asked))

    assert commit_count(repo) == 1
    assert channel._proposals[handle].message == "alpha-engine#281 power gen minor fixes"
    assert "alpha-engine#281 power gen minor fixes" in api.sent[-1]["text"]

    await channel._handle_callback(press(handle, cards.MAKE))
    assert subject(repo) == "alpha-engine#281 power gen minor fixes"


async def test_a_reply_to_a_commit_prompt_never_reaches_a_session(wired) -> None:
    """The hand-off that routes sentences to seats would otherwise claim this
    one — which is how `/to` sent two messages to an agent nobody chose."""
    channel, api, runner, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, cards.REWRITE))
    asked = api.sent[-1]["text"]
    before = len(runner.asked)

    await channel._handle_message(replying("power gen minor fixes", asked))

    assert len(runner.asked) == before


async def test_a_rewrite_of_a_proposal_that_expired_says_so(wired) -> None:
    channel, api, _, repo = wired
    stage(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, cards.REWRITE))
    asked = api.sent[-1]["text"]
    channel._proposals.clear()

    await channel._handle_message(replying("anything", asked))

    assert "no longer open" in api.sent[-1]["text"]
    assert commit_count(repo) == 1


# --- the command itself -----------------------------------------------------


def test_commit_is_registered_so_it_appears_when_you_type_a_slash() -> None:
    from halyard.channels.telegram.adapter import COMMANDS

    assert ("commit", "Commit what is staged, with a message to approve") in COMMANDS
