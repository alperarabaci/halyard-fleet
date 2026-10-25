"""GitHub, as far as `/label` needs it.

The second provider behind `Forge`, built the way `gitlab.py` is: everything
GitHub-shaped is here — the REST paths, the headers, `html_url` — and nothing
outside this module knows any of it.

**Labels are added, never set.** `POST …/issues/{number}/labels` with one name
adds it and leaves the rest, as GitHub documents it. `PUT` on the same path
replaces every label the issue has, and would quietly take off one somebody put
on at a desk between reading the issue and writing it — so it is never used.

**A label the repository lacks is made, either way.** `labels: who_worked`
writes labels like `navigator:claude` that often do not exist yet, and GitHub's
documentation does not say whether adding one the repository lacks creates it.
This was not measured. So when adding is refused and the label turns out not to
exist, it is created (`POST …/labels`) and added again; when GitHub makes it on
its own, nothing more happens.

**A colon is only a character.** GitHub has no scoped `::` labels, so
`level::3` and `navigator:claude` are plain names here.

The API is `https://api.github.com` for github.com, and `https://<host>/api/v3`
for GitHub Enterprise Server — a host that does not name itself, so the project
says `forge: github`. The token is a fine-grained one, limited to the
repository, with Issues: read and write, which is what GitHub's documentation
implies reading an issue, listing and creating labels and labelling an issue
need; that was not measured either.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from halyard.tasks.spec import ForgeError, Task

logger = logging.getLogger(__name__)

#: Long enough for a slow instance, short enough that a phone is told something.
TIMEOUT = 20.0

#: GitHub pages labels at 30 by default and at most 100 a page.
PER_PAGE = 100

#: Which version of the REST API this was written against.
API_VERSION = "2022-11-28"


class _RefusedError(ForgeError):
    """GitHub said no, with the status it said it with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class GitHub:
    """One GitHub repository."""

    name = "GitHub"

    def __init__(
        self, host: str, path: str, token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        # `owner/repo`, each part as it is: the slash between them is a path.
        self._repo = quote(path, safe="/")
        self._token = token
        self._client = client

    async def _ask(self, method: str, where: str, **kwargs) -> object:
        """One call, with GitHub's own words kept when it says no."""
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            answered = await client.request(
                method,
                f"{self._base}/repos/{self._repo}{where}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                **kwargs,
            )
        except httpx.HTTPError as unreachable:
            raise ForgeError(f"GitHub could not be reached: {unreachable}") from None
        finally:
            if self._client is None:
                await client.aclose()

        status = answered.status_code
        if status == 404:
            # What GitHub says for a private repository the token cannot see.
            raise _RefusedError(
                "GitHub has no such repository, issue or label — or the token cannot see it.",
                status,
            )
        if status >= 400:
            # Its own message, which is more use than anything invented here.
            said = _said(answered)
            head = {
                401: "GitHub refused the token",
                403: "That token is not allowed to do this on this repository",
            }.get(status, f"GitHub said {status}")
            raise _RefusedError(f"{head}: {said}" if said else head, status)
        try:
            return answered.json()
        except ValueError:
            raise ForgeError("GitHub answered with something that was not JSON.") from None

    async def task(self, number: int) -> Task:
        """The issue, with what is already on it."""
        return _as_task(await self._ask("GET", f"/issues/{number}"))

    async def labels(self) -> tuple[str, ...]:
        """Every label this repository defines."""
        body = await self._ask("GET", f"/labels?per_page={PER_PAGE}")
        if not isinstance(body, list):
            raise ForgeError("GitHub listed labels in a shape this does not understand.")
        return tuple(
            str(one.get("name")) for one in body if isinstance(one, dict) and one.get("name")
        )

    async def add_label(self, number: int, label: str) -> Task:
        """Add one label, leaving whatever else is on the issue alone — and
        make the label first if the repository does not have it."""
        try:
            await self._add(number, label)
        except _RefusedError as refused:
            if refused.status not in (404, 422) or await self._has(label):
                raise
            # The issue is read first, so a label is never made for one that
            # is not there.
            await self.task(number)
            await self._create(label)
            await self._add(number, label)
        return await self.task(number)

    async def _add(self, number: int, label: str) -> None:
        await self._ask("POST", f"/issues/{number}/labels", json={"labels": [label]})

    async def _has(self, label: str) -> bool:
        """Whether the repository defines this label."""
        try:
            await self._ask("GET", f"/labels/{quote(label, safe='')}")
        except _RefusedError as refused:
            if refused.status == 404:
                return False
            raise
        return True

    async def _create(self, label: str) -> None:
        """Make the label. One somebody made in the meantime is as good."""
        try:
            await self._ask("POST", "/labels", json={"name": label})
        except _RefusedError as refused:
            if refused.status != 422:
                raise
        logger.info("Made the label %s in %s", label, self._repo)


def _said(answered: httpx.Response) -> str:
    """GitHub's own message, with the first detail it gives, if any."""
    try:
        body = answered.json()
    except ValueError:
        return answered.text[:200]
    if not isinstance(body, dict):
        return ""
    said = str(body.get("message") or "")
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        detail = errors[0].get("message") or errors[0].get("code")
        if detail:
            said = f"{said} ({detail})" if said else str(detail)
    return said


def _as_task(body: object) -> Task:
    if not isinstance(body, dict):
        raise ForgeError("GitHub described that issue in a shape this does not understand.")
    labels = body.get("labels") or []
    names = (one.get("name") if isinstance(one, dict) else one for one in labels)
    return Task(
        number=int(body.get("number") or 0),
        title=str(body.get("title") or ""),
        labels=tuple(str(name) for name in names if name and str(name).strip()),
        url=str(body.get("html_url") or ""),
    )
