"""Tests for `/commit` — the Telegram half.

`tests/test_commit_repository.py` covers what git is asked and how its answers are read.
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

from halyard.channels.telegram import commit_card
from halyard.channels.telegram.adapter import TelegramChannel
from halyard.core.approvals import ApprovalStore
from halyard.core.audit import AuditLog, JsonlAuditSink
from halyard.core.config_file import Project
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


# --- proposing --------------------------------------------------------------


async def test_a_card_offers_the_message_and_what_it_would_commit(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "def load():\n    return 1\n")

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
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert runner.models == ["sonnet"]
    assert "alpha-engine#279 p2" in runner.asked[0]


async def test_what_an_agent_wrote_without_staging_is_offered(wired) -> None:
    """The case the first version got wrong: nobody staged anything, and there
    is still an afternoon of work to commit."""
    channel, api, _, repo = wired
    (repo / "written_by_an_agent.py").write_text("def load():\n    return 1\n")

    await channel._handle_message(typed("/commit"))

    assert "written_by_an_agent.py" in api.sent[-1]["text"]
    assert "1 of them new" in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


async def test_a_clean_branch_says_there_is_nothing_to_commit(wired) -> None:
    channel, api, _, repo = wired

    await channel._handle_message(typed("/commit"))

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
        await channel._handle_message(typed("/commit"))
        assert "do not know which repository" in api.sent[-1]["text"]
    finally:
        await audit.close()


async def test_a_model_that_cannot_be_reached_still_offers_the_reference(wired) -> None:
    """Failing soft. The branch already names the issue, and Rewrite is one tap
    away — losing the commit because a model was busy would be the worse end."""
    channel, api, runner, repo = wired
    runner.says = None
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert "alpha-engine#281" in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


# --- committing -------------------------------------------------------------


async def test_pressing_commit_makes_the_commit(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE))

    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"
    assert commit_count(repo) == 2
    assert "COMMITTED" in api.edits[-1]["text"]


async def test_pressing_commit_twice_makes_one_commit(wired) -> None:
    """There is no nonce here — the proposal is dropped as it is used, and that
    is what a second tap runs into."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    await channel._handle_callback(press(handle, commit_card.MAKE))
    await channel._handle_callback(press(handle, commit_card.MAKE))

    assert commit_count(repo) == 2
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_cancel_leaves_the_repository_alone(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.DROP))

    assert commit_count(repo) == 1
    assert "CANCELLED" in api.edits[-1]["text"]
    assert len(channel._proposals) == 0


async def test_somebody_else_pressing_commit_commits_nothing(wired) -> None:
    """The card is visible to a whole group. Checked exactly as an approval is."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE, user=INTRUDER))

    assert commit_count(repo) == 1
    assert len(channel._proposals) == 1
    assert api.edits == []


async def test_a_proposal_left_too_long_is_not_committable(wired) -> None:
    """The card describes a staging area as it was. A button tapped tomorrow
    would commit whatever is staged then, under a message written for this."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    channel._proposals._open[handle] = replace(
        channel._proposals._open[handle], at=channel._clock() - timedelta(hours=2)
    )
    await channel._handle_callback(press(handle, commit_card.MAKE))

    assert commit_count(repo) == 1
    assert api.answers[-1]["text"] == "That commit is no longer open."


async def test_git_refusing_is_reported_rather_than_swallowed(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    # Taken away behind the card's back, so there is nothing left to commit.
    (repo / "loader.py").unlink()

    await channel._handle_callback(press(handle, commit_card.MAKE))

    assert commit_count(repo) == 1
    assert "git refused" in api.sent[-1]["text"]


# --- rewriting --------------------------------------------------------------


async def test_rewrite_asks_for_a_message_and_commits_nothing(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)

    await channel._handle_callback(press(handle, commit_card.REWRITE))

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
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]

    await channel._handle_message(replying("power gen minor fixes", asked))

    assert commit_count(repo) == 1
    assert channel._proposals.peek(handle).message == "alpha-engine#281 power gen minor fixes"
    assert "alpha-engine#281 power gen minor fixes" in api.sent[-1]["text"]

    await channel._handle_callback(press(handle, commit_card.MAKE))
    assert subject(repo) == "alpha-engine#281 power gen minor fixes"


async def test_a_reply_to_a_commit_prompt_never_reaches_a_session(wired) -> None:
    """The hand-off that routes sentences to seats would otherwise claim this
    one — which is how `/to` sent two messages to an agent nobody chose."""
    channel, api, runner, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]
    before = len(runner.asked)

    await channel._handle_message(replying("power gen minor fixes", asked))

    assert len(runner.asked) == before


async def test_a_rewrite_of_a_proposal_that_expired_says_so(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, commit_card.REWRITE))
    asked = api.sent[-1]["text"]
    channel._proposals._open.clear()

    await channel._handle_message(replying("anything", asked))

    assert "no longer open" in api.sent[-1]["text"]
    assert commit_count(repo) == 1


# --- the command itself -----------------------------------------------------


def test_commit_is_registered_so_it_appears_when_you_type_a_slash() -> None:
    from halyard.channels.telegram.adapter import COMMANDS

    assert ("commit", "Commit this branch's work, with a message to approve") in COMMANDS


# --- saying it happened, and pushing ----------------------------------------


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
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE))

    said = api.sent[-1]["text"]
    assert "Committed" in said
    assert "281-power-gen-minor-fixes" in said
    assert "alpha-engine#281 loader stub and seed tweak" in said


async def test_commit_and_push_sends_the_branch(tmp_path: Path, wired) -> None:
    channel, api, _, repo = wired
    remote = a_bare_remote(tmp_path, repo)
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.SEND))

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
    await channel._handle_message(typed("/commit"))

    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE))

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
    await channel._handle_message(typed("/commit"))

    # No remote at all, so `git push` refuses.
    await channel._handle_callback(press(only_handle(channel), commit_card.SEND))

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

    await channel._handle_message(typed("/commit"))

    card = api.sent[-1]["text"]
    assert "Adds a loader that returns the generated figure" in card
    assert "Leaves the existing callers untouched" in card
    # The summary is for the card. The commit keeps this project's one-line
    # subjects rather than growing a body nobody's history has ever had.
    assert "alpha-engine#281 loader stub and seed tweak" in card
    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE))
    assert subject(repo) == "alpha-engine#281 loader stub and seed tweak"
    assert "Adds a loader" not in git(repo, "log", "-1", "--format=%B")


# --- what has to hold before a card is offered -------------------------------


def demands(channel: TelegramChannel, command: str | None) -> None:
    """Give the project a check to run, as `halyard.yaml` would."""
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, validate=command)


async def test_a_project_with_no_check_configured_runs_nothing(wired) -> None:
    """Absent means absent. A command invented on a project's behalf would fail
    on every commit."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert "Running" not in " ".join(m["text"] for m in api.sent)
    assert len(channel._proposals) == 1


async def test_a_failing_check_offers_no_card_at_all(wired) -> None:
    """A failing check is a fact, not a judgement — there is nothing here for
    somebody to weigh, so nothing is put in front of them."""
    channel, api, runner, repo = wired
    demands(channel, "echo 'FAIL: two tests broke' && exit 1")
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

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

    await channel._handle_message(typed("/commit"))

    assert "Running" in api.sent[0]["text"]
    assert "true" in api.sent[0]["text"]


async def test_a_passing_check_leads_to_an_ordinary_card(wired) -> None:
    channel, api, _, repo = wired
    demands(channel, "true")
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert len(channel._proposals) == 1
    assert "alpha-engine#281" in api.sent[-1]["text"]


async def test_the_check_runs_in_the_project_and_sees_its_files(wired) -> None:
    channel, _, _, repo = wired
    demands(channel, "test -f loader.py")
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert len(channel._proposals) == 1


async def test_a_task_id_missing_from_the_code_warns_without_blocking(wired) -> None:
    """A guess, treated like one. An agent that has lost the thread leaves
    references to its own conversation in the code; the branch's number showing
    up in what was written is a cheap sign it did not. A rename or a .gitignore
    fix will never mention it and is perfectly good, so this warns."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    card = api.sent[-1]["text"]
    assert "281 appears nowhere" in card
    assert card.index("281 appears nowhere") < card.index("COMMIT")  # above the heading
    assert len(channel._proposals) == 1

    await channel._handle_callback(press(only_handle(channel), commit_card.MAKE))
    assert commit_count(repo) == 2


async def test_work_that_names_its_task_is_not_flagged(wired) -> None:
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "# alpha-engine#281 — the loader this task asked for\nx = 1\n")

    await channel._handle_message(typed("/commit"))

    assert "appears nowhere" not in api.sent[-1]["text"]


async def test_the_warning_survives_rewording_the_message(wired) -> None:
    """A warning that disappears when you type is a warning nobody heeds twice."""
    channel, api, _, repo = wired
    wrote(repo, "loader.py", "x = 1\n")
    await channel._handle_message(typed("/commit"))
    handle = only_handle(channel)
    await channel._handle_callback(press(handle, commit_card.REWRITE))

    await channel._handle_message(replying("power gen fixes", api.sent[-1]["text"]))

    assert "281 appears nowhere" in api.sent[-1]["text"]


async def test_a_project_can_turn_the_warnings_off(wired) -> None:
    """The task-id check is this project's house style, not a truth about
    software. Somebody who does not share it says `warn_if: []`."""
    channel, api, _, repo = wired
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, warn_if=())
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert "appears nowhere" not in api.sent[-1]["text"]
    assert len(channel._proposals) == 1


async def test_a_warning_nobody_recognises_is_skipped_not_fatal(wired) -> None:
    """A typo in a list of opinions must not cost the ability to commit."""
    channel, _, _, repo = wired
    found = channel._repositories["alpha-engine"]
    channel._repositories["alpha-engine"] = replace(found, warn_if=("no-such-check",))
    wrote(repo, "loader.py", "x = 1\n")

    await channel._handle_message(typed("/commit"))

    assert len(channel._proposals) == 1
