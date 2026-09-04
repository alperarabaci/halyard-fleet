"""Tests for labelling the task a branch is for.

GitLab is exercised through `httpx`'s own transport rather than a hand-written
double, so the URLs, the header and the request body are all really built. What
is faked is the network and nothing else — a stub with a `labels()` method would
pass whatever this file believed and prove nothing about the API.

The shapes below are GitLab's: `iid` rather than `id` for the number a person
sees, `labels` as a list of names, `web_url` for where it lives.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from halyard.tasks import branches, labelling, registry, remotes
from halyard.tasks.gitlab import GitLab
from halyard.tasks.spec import Forge, ForgeError, Task

ORIGIN = remotes.Origin(host="gitlab.com", path="agent-platform34/investment/alpha-engine")
ENCODED = "agent-platform34%2Finvestment%2Falpha-engine"


class Answering:
    """Stands in for the network, recording what was asked of it."""

    def __init__(self, **routes) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        for fragment, (status, body) in self.routes.items():
            if fragment in str(request.url):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"message": "404 Not found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


ISSUE = {
    "iid": 320,
    "title": "RAG v4 PDF report improvement",
    "labels": ["backend"],
    "web_url": "https://gitlab.com/x/-/issues/320",
}


def forge(answering: Answering) -> GitLab:
    return GitLab(ORIGIN.host, ORIGIN.path, "glpat-x", client=answering.client())


# --- reading a remote -------------------------------------------------------


def test_both_spellings_of_a_remote_are_read() -> None:
    """A repository is cloned either way and the same person switches."""
    ssh = remotes.read("git@gitlab.com:agent-platform34/investment/alpha-engine.git")
    https = remotes.read("https://gitlab.com/agent-platform34/investment/alpha-engine.git")

    assert ssh == https == ORIGIN


def test_credentials_and_ports_in_a_remote_are_not_carried_along() -> None:
    found = remotes.read("https://someone:secret@git.example.com:8443/team/thing.git")

    assert found == remotes.Origin(host="git.example.com", path="team/thing")


@pytest.mark.parametrize("remote", ["", "   ", "/just/a/path", "not a remote"])
def test_something_that_is_not_a_remote_reads_as_nothing(remote: str) -> None:
    assert remotes.read(remote) is None


def test_the_remote_is_asked_of_git_rather_than_of_configuration(tmp_path: Path) -> None:
    """So a repository whose remote changed this morning is labelled against
    the project it is now part of."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "git@gitlab.com:a/b.git"],
        check=True,
    )

    assert remotes.origin_of(tmp_path) == remotes.Origin(host="gitlab.com", path="a/b")


def test_a_checkout_with_no_remote_answers_nothing(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert remotes.origin_of(tmp_path) is None


# --- which task a branch is for ---------------------------------------------


def test_a_branch_named_for_an_issue_gives_its_number() -> None:
    assert branches.number_of("320-rag-v4-pdf-report-improvement-p3") == 320
    assert branches.number_of("281_power_gen") == 281


def test_a_branch_not_named_for_one_gives_nothing() -> None:
    assert branches.number_of("feat/runtime-isolation") is None
    assert branches.number_of("release-2026") is None
    assert branches.number_of("") is None


def test_the_branch_is_read_off_the_checkout(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "320-thing", str(tmp_path)], check=True)

    assert branches.current(tmp_path) == "320-thing"


# --- which forge ------------------------------------------------------------


def test_a_host_that_names_itself_needs_no_configuration() -> None:
    assert registry.kind_of(ORIGIN) == "gitlab"


def test_a_host_that_cannot_name_itself_has_to_be_told() -> None:
    """Nothing in `git.example.com` says whether it is GitLab or something else."""
    private = remotes.Origin(host="git.example.com", path="team/thing")

    assert registry.kind_of(private) is None
    assert registry.kind_of(private, declared="gitlab") == "gitlab"
    assert isinstance(registry.build(private, "t", declared="gitlab"), Forge)


def test_an_unknown_host_says_what_to_write() -> None:
    with pytest.raises(ForgeError, match="forge:"):
        registry.build(remotes.Origin(host="git.example.com", path="a/b"), "t")


def test_a_provider_nobody_has_written_yet_says_so() -> None:
    with pytest.raises(ForgeError, match="No support for 'github'"):
        registry.build(ORIGIN, "t", declared="github")


def test_no_token_is_its_own_answer() -> None:
    with pytest.raises(ForgeError, match="No token"):
        registry.build(ORIGIN, "")


# --- talking to GitLab ------------------------------------------------------


async def test_an_issue_is_read_with_what_is_already_on_it() -> None:
    answering = Answering(**{"/issues/320": (200, ISSUE)})

    task = await forge(answering).task(320)

    assert (task.number, task.title, task.labels) == (320, ISSUE["title"], ("backend",))
    assert ENCODED in str(answering.seen[0].url)
    assert answering.seen[0].headers["PRIVATE-TOKEN"] == "glpat-x"


async def test_labels_are_listed_by_name() -> None:
    answering = Answering(
        **{"/labels": (200, [{"name": "andon"}, {"name": "rework"}, {"nope": 1}])}
    )

    assert await forge(answering).labels() == ("andon", "rework")


async def test_a_label_is_added_rather_than_the_set_replaced() -> None:
    """Between reading an issue and writing it back, somebody at a desk may
    have labelled it themselves. A full write would quietly remove that."""
    answering = Answering(**{"/issues/320": (200, {**ISSUE, "labels": ["backend", "andon"]})})

    task = await forge(answering).add_label(320, "andon")

    sent = json.loads(answering.seen[-1].content)
    assert sent == {"add_labels": "andon"}
    assert answering.seen[-1].method == "PUT"
    assert task.labels == ("backend", "andon")


async def test_a_refused_token_says_which_problem_it_is() -> None:
    answering = Answering(**{"/issues/320": (401, {"message": "401 Unauthorized"})})

    with pytest.raises(ForgeError, match="api"):
        await forge(answering).task(320)


async def test_a_forbidden_token_is_told_apart_from_a_missing_one() -> None:
    answering = Answering(**{"/issues/320": (403, {"message": "403 Forbidden"})})

    with pytest.raises(ForgeError, match="not allowed"):
        await forge(answering).task(320)


async def test_a_missing_issue_says_so() -> None:
    answering = Answering()

    with pytest.raises(ForgeError, match="no such project or issue"):
        await forge(answering).task(999)


async def test_gitlabs_own_words_are_kept_for_anything_else() -> None:
    answering = Answering(**{"/issues/320": (422, {"message": "Label does not exist"})})

    with pytest.raises(ForgeError, match="Label does not exist"):
        await forge(answering).task(320)


async def test_a_network_that_is_not_there_is_answered_not_raised_raw() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening")

    broken = GitLab(
        ORIGIN.host,
        ORIGIN.path,
        "t",
        client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )

    with pytest.raises(ForgeError, match="could not be reached"):
        await broken.task(320)


# --- what is worth offering -------------------------------------------------


def a_task(*labels: str) -> Task:
    return Task(number=320, title="RAG v4 PDF report", labels=tuple(labels))


def test_a_label_already_on_the_task_is_not_offered() -> None:
    """Which also means the "it is already there" case cannot arise, so nothing
    downstream has to handle it."""
    offered = labelling.worth_offering(a_task("backend"), ["andon", "rework", "backend"])

    assert offered == ("andon", "rework")


def test_it_does_not_care_how_a_label_was_capitalised() -> None:
    """A forge lets `Andon` and `andon` be one label to a person and two
    different strings to a comparison."""
    assert labelling.worth_offering(a_task("Andon"), ["andon", "rework"]) == ("rework",)


def test_a_project_can_narrow_what_is_offered() -> None:
    offered = labelling.worth_offering(
        a_task(), ["andon", "rework", "wontfix", "duplicate"], narrow=["andon", "rework"]
    )

    assert offered == ("andon", "rework")


def test_narrowing_to_nothing_means_everything() -> None:
    """Empty is the right default until a project has more labels than a phone
    can show."""
    assert labelling.worth_offering(a_task(), ["andon", "rework"]) == ("andon", "rework")


def test_the_forge_order_is_kept() -> None:
    """A forge lists labels in an order somebody chose; re-sorting them would
    move the button somebody's thumb already knows."""
    assert labelling.worth_offering(a_task(), ["zebra", "andon"]) == ("zebra", "andon")


async def test_an_offer_reads_the_task_and_the_labels_together() -> None:
    answering = Answering(
        **{
            "/issues/320": (200, ISSUE),
            "/labels": (200, [{"name": "andon"}, {"name": "backend"}]),
        }
    )

    choice = await labelling.to_offer(forge(answering), 320)

    assert choice.task.number == 320
    assert choice.offer == ("andon",)
    assert choice.anything_left is True


async def test_a_task_wearing_everything_leaves_nothing_to_offer() -> None:
    answering = Answering(**{"/issues/320": (200, ISSUE), "/labels": (200, [{"name": "backend"}])})

    choice = await labelling.to_offer(forge(answering), 320)

    assert choice.offer == ()
    assert choice.anything_left is False
