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
    assert isinstance(registry.build(private, {"gitlab": "t"}, declared="gitlab"), Forge)


def test_an_unknown_host_says_what_to_write() -> None:
    with pytest.raises(ForgeError, match="`forge: github` or `forge: gitlab`"):
        registry.build(remotes.Origin(host="git.example.com", path="a/b"), {"gitlab": "t"})


def test_a_provider_nobody_has_written_yet_says_so() -> None:
    with pytest.raises(ForgeError, match=r"No support for 'gitea'.*`forge: github`"):
        registry.build(ORIGIN, {"gitlab": "t"}, declared="gitea")


def test_no_token_is_its_own_answer() -> None:
    with pytest.raises(
        ForgeError, match=r"No GitLab token for gitlab\.com: set HALYARD_GITLAB_TOKEN"
    ):
        registry.build(ORIGIN, {})


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


def test_a_task_carries_the_first_label_of_each_group_it_has_one_from() -> None:
    """`level: level::3` from a task labelled `backend` and `level::3`; a group
    the task carries nothing from is left out rather than guessed at."""
    groups = {"level": ("level::1", "level::2", "level::3"), "risk": ("risk::high",)}

    assert labelling.picked(groups, ("backend", "level::3")) == {"level": "level::3"}


def test_two_labels_from_one_group_give_the_first_in_the_groups_order() -> None:
    """There should never be two; if there are, the answer is still one, and
    always the same one."""
    groups = {"level": ("level::1", "level::2", "level::3")}

    assert labelling.picked(groups, ("level::3", "level::1")) == {"level": "level::1"}


def test_a_group_label_is_found_however_the_task_capitalised_it() -> None:
    assert labelling.picked({"level": ("level::3",)}, ("Level::3",)) == {"level": "Level::3"}


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


def test_a_project_can_leave_labels_out_by_pattern() -> None:
    """The ones nobody puts on a task by hand, whatever their case."""
    offered = labelling.worth_offering(
        a_task(),
        ["andon", "halyard:proof", "Halyard:Claims", "rework"],
        exclude=["halyard:*"],
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


# --- talking to GitHub ------------------------------------------------------

GITHUB = remotes.Origin(host="github.com", path="alperarabaci/halyard-fleet")
REPO = "https://api.github.com/repos/alperarabaci/halyard-fleet"

#: GitHub's shape: `number`, labels as objects with a `name`, `html_url`.
GH_ISSUE = {
    "number": 42,
    "title": "Label tasks on GitHub",
    "labels": [{"id": 1, "name": "backend", "color": "ededed"}],
    "html_url": "https://github.com/alperarabaci/halyard-fleet/issues/42",
}


class GitHubAnswering:
    """Stands in for GitHub's API: routes by method and path, each answered
    in turn by the next of its responses, and every request recorded."""

    def __init__(self, routes: dict[tuple[str, str], list[tuple[int, object]]]) -> None:
        self.routes = {key: list(answers) for key, answers in routes.items()}
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answers = self.routes.get((request.method, request.url.raw_path.decode()), [])
        if not answers:
            return httpx.Response(404, json={"message": "Not Found"})
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def asked(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.raw_path.decode()) for r in self.seen]


def github(answering: GitHubAnswering, host: str = "github.com", path: str = GITHUB.path):
    from halyard.tasks.github import GitHub

    return GitHub(host, path, "github_pat_x", client=answering.client())


PATH = "/repos/alperarabaci/halyard-fleet"


async def test_a_github_issue_is_read_with_its_labels_and_where_it_lives() -> None:
    answering = GitHubAnswering({("GET", f"{PATH}/issues/42"): [(200, GH_ISSUE)]})

    task = await github(answering).task(42)

    assert task == Task(42, GH_ISSUE["title"], ("backend",), GH_ISSUE["html_url"])
    [request] = answering.seen
    assert str(request.url) == f"{REPO}/issues/42"
    assert request.headers["Authorization"] == "Bearer github_pat_x"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


async def test_github_labels_are_listed_a_hundred_to_a_page() -> None:
    answering = GitHubAnswering(
        {("GET", f"{PATH}/labels?per_page=100"): [(200, [{"name": "andon"}, {"name": "a::b"}])]}
    )

    assert await github(answering).labels() == ("andon", "a::b")
    assert answering.seen[0].method == "GET"


async def test_a_github_label_is_added_and_the_rest_left_alone() -> None:
    """`POST`, which adds — never `PUT`, which replaces every label the issue
    has. Colons are part of the name: GitHub has no scoped labels."""
    labelled = {**GH_ISSUE, "labels": [*GH_ISSUE["labels"], {"name": "navigator:claude"}]}
    answering = GitHubAnswering(
        {
            ("POST", f"{PATH}/issues/42/labels"): [(200, [{"name": "navigator:claude"}])],
            ("GET", f"{PATH}/issues/42"): [(200, labelled)],
        }
    )

    task = await github(answering).add_label(42, "navigator:claude")

    assert answering.asked() == [("POST", f"{PATH}/issues/42/labels"), ("GET", f"{PATH}/issues/42")]
    assert json.loads(answering.seen[0].content) == {"labels": ["navigator:claude"]}
    assert task.labels == ("backend", "navigator:claude")


async def test_a_label_the_repository_lacks_is_made_then_added() -> None:
    """Whether GitHub makes it on its own was not measured, so a refusal
    because it does not exist is answered by making it and adding it again."""
    labelled = {**GH_ISSUE, "labels": [{"name": "navigator:claude"}]}
    answering = GitHubAnswering(
        {
            ("POST", f"{PATH}/issues/42/labels"): [
                (422, {"message": "Validation Failed", "errors": [{"code": "invalid"}]}),
                (200, [{"name": "navigator:claude"}]),
            ],
            ("GET", f"{PATH}/labels/navigator%3Aclaude"): [(404, {"message": "Not Found"})],
            ("GET", f"{PATH}/issues/42"): [(200, GH_ISSUE), (200, labelled)],
            ("POST", f"{PATH}/labels"): [(201, {"name": "navigator:claude"})],
        }
    )

    task = await github(answering).add_label(42, "navigator:claude")

    assert answering.asked() == [
        ("POST", f"{PATH}/issues/42/labels"),
        ("GET", f"{PATH}/labels/navigator%3Aclaude"),
        ("GET", f"{PATH}/issues/42"),
        ("POST", f"{PATH}/labels"),
        ("POST", f"{PATH}/issues/42/labels"),
        ("GET", f"{PATH}/issues/42"),
    ]
    assert json.loads(answering.seen[3].content) == {"name": "navigator:claude"}
    assert task.labels == ("navigator:claude",)


async def test_a_label_somebody_made_in_the_meantime_is_still_added() -> None:
    answering = GitHubAnswering(
        {
            ("POST", f"{PATH}/issues/42/labels"): [(404, {}), (200, [])],
            ("GET", f"{PATH}/labels/andon"): [(404, {"message": "Not Found"})],
            ("GET", f"{PATH}/issues/42"): [(200, GH_ISSUE)],
            ("POST", f"{PATH}/labels"): [
                (422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
            ],
        }
    )

    await github(answering).add_label(42, "andon")

    assert answering.asked().count(("POST", f"{PATH}/issues/42/labels")) == 2


async def test_a_refusal_for_a_label_that_exists_is_not_answered_by_making_it() -> None:
    answering = GitHubAnswering(
        {
            ("POST", f"{PATH}/issues/42/labels"): [(422, {"message": "Validation Failed"})],
            ("GET", f"{PATH}/labels/andon"): [(200, {"name": "andon"})],
        }
    )

    with pytest.raises(ForgeError, match="GitHub said 422: Validation Failed"):
        await github(answering).add_label(42, "andon")

    assert ("POST", f"{PATH}/labels") not in answering.asked()


async def test_no_label_is_made_for_an_issue_that_is_not_there() -> None:
    answering = GitHubAnswering(
        {
            ("POST", f"{PATH}/issues/999/labels"): [(404, {"message": "Not Found"})],
            ("GET", f"{PATH}/labels/andon"): [(404, {"message": "Not Found"})],
        }
    )

    with pytest.raises(ForgeError, match="no such repository, issue or label"):
        await github(answering).add_label(999, "andon")

    assert ("POST", f"{PATH}/labels") not in answering.asked()


async def test_github_enterprise_is_reached_under_its_own_host() -> None:
    answering = GitHubAnswering({("GET", "/api/v3/repos/team/thing/issues/42"): [(200, GH_ISSUE)]})

    await github(answering, host="ghe.example.com", path="team/thing").task(42)

    assert str(answering.seen[0].url) == "https://ghe.example.com/api/v3/repos/team/thing/issues/42"


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (401, {"message": "Bad credentials"}, "GitHub refused the token: Bad credentials"),
        (
            403,
            {"message": "Resource not accessible by personal access token"},
            "That token is not allowed to do this on this repository: "
            "Resource not accessible by personal access token",
        ),
        (404, {"message": "Not Found"}, "GitHub has no such repository, issue or label"),
        (500, {"message": "Server Error"}, "GitHub said 500: Server Error"),
        (
            422,
            {"message": "Validation Failed", "errors": [{"message": "name is too long"}]},
            "GitHub said 422: Validation Failed (name is too long)",
        ),
    ],
)
async def test_githubs_own_words_come_back_as_a_forge_error(status, body, said) -> None:
    answering = GitHubAnswering({("GET", f"{PATH}/issues/42"): [(status, body)]})

    with pytest.raises(ForgeError) as refused:
        await github(answering).task(42)

    assert said in str(refused.value)


async def test_a_github_that_is_not_there_is_answered_not_raised_raw() -> None:
    from halyard.tasks.github import GitHub

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening")

    broken = GitHub(
        "github.com", "a/b", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )

    with pytest.raises(ForgeError, match="GitHub could not be reached"):
        await broken.task(1)


def test_github_com_names_itself_and_an_enterprise_host_is_told() -> None:
    from halyard.tasks.github import GitHub

    enterprise = remotes.Origin(host="ghe.example.com", path="team/thing")

    assert registry.kind_of(GITHUB) == "github"
    assert isinstance(registry.build(GITHUB, {"github": "t"}), GitHub)
    assert registry.kind_of(enterprise) is None
    assert isinstance(registry.build(enterprise, {"github": "t"}, declared="github"), GitHub)


def test_each_forge_is_built_with_its_own_token_alone() -> None:
    """One machine can label a GitHub project and a GitLab one; neither token
    reaches the other forge."""
    tokens = {"github": "github_pat_x", "gitlab": "glpat-x"}

    assert registry.build(GITHUB, tokens)._token == "github_pat_x"
    assert registry.build(ORIGIN, tokens)._token == "glpat-x"
    with pytest.raises(
        ForgeError, match=r"No GitHub token for github\.com: set HALYARD_GITHUB_TOKEN"
    ):
        registry.build(GITHUB, {"gitlab": "glpat-x"})


def test_a_github_branch_named_for_an_issue_gives_its_number() -> None:
    """GitHub names a branch made from an issue `<number>-<title-slug>`."""
    assert branches.number_of("42-label-tasks-on-github") == 42
